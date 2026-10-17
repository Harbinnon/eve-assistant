"""Small charts drawn on the server as SVG: the pages run no scripts."""

from dataclasses import dataclass
from datetime import datetime

WIDTH, HEIGHT, PAD = 600, 120, 4


@dataclass(frozen=True)
class Line:
    points: str  # for <polyline points="...">
    width: int
    height: int
    first: float
    last: float
    low: float
    high: float
    since: str  # the first point's date, YYYY-MM-DD

    @property
    def change(self) -> float:
        return self.last - self.first


def line(series: list[tuple[str, float]], width: int = WIDTH, height: int = HEIGHT) -> Line | None:
    """A line through (ISO time, value) pairs, spaced by time; None under two points."""
    if len(series) < 2:
        return None
    times = [datetime.fromisoformat(at).timestamp() for at, _ in series]
    values = [value for _, value in series]
    start, low, high = min(times), min(values), max(values)
    across = (max(times) - start) or 1.0
    up = high - low
    coords = []
    for moment, value in zip(times, values, strict=True):
        x = PAD + (moment - start) / across * (width - 2 * PAD)
        y = height / 2 if not up else height - PAD - (value - low) / up * (height - 2 * PAD)
        coords.append(f"{x:.1f},{y:.1f}")
    return Line(
        " ".join(coords), width, height, values[0], values[-1], low, high, series[0][0][:10]
    )
