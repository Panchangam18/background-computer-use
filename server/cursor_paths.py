"""Natural mouse-path generation.

Produces samples along a believable arc between two screen points so the
agent's ghost cursor can be animated from where it was last to where it's
about to click. The path is a cubic Bezier with randomized perpendicular
control-point offsets (so each move has a slightly different arc), driven
by an ease-out time parameter (peak speed early, decelerate into target --
matches how humans point with a mouse).

Pure Python, no AppKit dependencies, so this is trivially unit-testable
without a display.
"""
from __future__ import annotations

import math
import random


def duration_for_distance(distance: float) -> float:
    """Fitts-like: short moves are snappy, long moves take longer, capped.

    Values tuned so a ~1500px move is ~600ms and a ~50px move is ~140ms.
    Callers can always override with an explicit duration.
    """
    if distance < 4.0:
        return 0.04
    d = 0.14 + 0.11 * math.log2(1.0 + distance / 45.0)
    return max(0.14, min(0.75, d))


def _ease_out_cubic(t: float) -> float:
    return 1.0 - (1.0 - t) ** 3


def natural_path(
    x0: float,
    y0: float,
    x1: float,
    y1: float,
    *,
    duration: float | None = None,
    fps: int = 90,
    jitter: float = 0.7,
    rng: random.Random | None = None,
) -> list[tuple[float, float, float]]:
    """Return ``(x, y, t_seconds)`` samples from ``(x0, y0)`` to ``(x1, y1)``.

    The path is a cubic Bezier with perpendicular control-point offsets that
    vary randomly per call, so repeated moves between the same points don't
    trace identical lines. Time parameter uses ease-out cubic so the ghost
    decelerates into the target (natural pointing motion).

    Args:
        duration: Total animation time (seconds). If ``None``, picked from
            the distance via ``duration_for_distance``.
        fps: Target samples per second. ~90 is smooth on Retina displays
            without flooding the daemon with redraws.
        jitter: Amplitude (px) of high-frequency wobble added to each
            sample, 0 = perfectly smooth Bezier. Real pointing has small
            wobble from finger micro-tremor, hence the default > 0.
        rng: Optional seeded RNG for reproducibility (e.g. from tests).
    """
    rng = rng or random
    dx = x1 - x0
    dy = y1 - y0
    dist = math.hypot(dx, dy)
    if dist < 1.5:
        return [(x1, y1, 0.0)]

    total = duration if duration is not None else duration_for_distance(dist)

    # Unit perpendicular vector.
    perp_x = -dy / dist
    perp_y = dx / dist

    # Curvature amplitude: proportional to distance so long moves arc more,
    # plus jitter so consecutive moves don't look mechanically identical.
    # Random sign makes some paths arc left of the direct line, others right.
    magnitude = dist * (0.10 + 0.08 * rng.random())
    side = 1.0 if rng.random() < 0.5 else -1.0
    curve = magnitude * side

    # Two control points near the 1/3 and 2/3 marks, offset perpendicularly
    # by unequal amounts so the arc is asymmetric (more human-looking than
    # a perfectly symmetric curve).
    cp1x = x0 + 0.33 * dx + curve * 1.00 * perp_x
    cp1y = y0 + 0.33 * dy + curve * 1.00 * perp_y
    cp2x = x0 + 0.67 * dx + curve * 0.55 * perp_x
    cp2y = y0 + 0.67 * dy + curve * 0.55 * perp_y

    steps = max(8, int(total * fps))
    # Two out-of-phase sine components for the jitter give smooth wobble
    # that's not obviously periodic.
    j_freq_a = 6.0 + rng.random() * 3.0
    j_freq_b = 11.0 + rng.random() * 5.0
    j_phase_a = rng.random() * math.tau
    j_phase_b = rng.random() * math.tau

    path: list[tuple[float, float, float]] = []
    for i in range(steps + 1):
        u = i / steps  # 0 .. 1
        t = _ease_out_cubic(u)
        mt = 1.0 - t

        bx = mt**3 * x0 + 3 * mt**2 * t * cp1x + 3 * mt * t**2 * cp2x + t**3 * x1
        by = mt**3 * y0 + 3 * mt**2 * t * cp1y + 3 * mt * t**2 * cp2y + t**3 * y1

        # Jitter tapers to zero at both ends so the start and landing
        # positions are exact (no sub-pixel drift at the click target).
        taper = math.sin(math.pi * u)
        jx = jitter * taper * math.sin(j_freq_a * u * math.tau + j_phase_a)
        jy = jitter * taper * math.cos(j_freq_b * u * math.tau + j_phase_b)

        path.append((bx + jx, by + jy, u * total))
    return path


if __name__ == "__main__":  # pragma: no cover
    import sys

    # Quick smoke test: print a path from (0, 0) to the given point.
    try:
        x = float(sys.argv[1])
        y = float(sys.argv[2])
    except (IndexError, ValueError):
        x, y = 800.0, 400.0
    for px, py, pt in natural_path(0.0, 0.0, x, y):
        print(f"t={pt*1000:6.1f}ms  x={px:7.2f}  y={py:7.2f}")
