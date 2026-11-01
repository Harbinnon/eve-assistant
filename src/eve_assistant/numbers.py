"""Numbers as the dashboard and explanations show them, so every page and
every reason writes ISK the same way."""

import math

UNITS = ((1e9, "B"), (1e6, "M"), (1e3, "k"))
# 999,500 rounds to 1.00M, not "1e+03k": move up a unit just before the limit.
ROUND_UP_FROM = 0.9995


def isk(value: float | None) -> str:
    """ISK to three significant figures: 2.46B, 12.3M, 929M, 640k, 950."""
    if value is None:
        return "?"
    for limit, suffix in UNITS:
        if abs(value) >= limit * ROUND_UP_FROM:
            scaled = value / limit
            return (f"{scaled:,.0f}" if abs(scaled) >= 100 else f"{scaled:.3g}") + suffix
    return f"{value:,.0f}"


SUFFIXES = {"k": 1e3, "m": 1e6, "b": 1e9}


def parse_isk(text: str) -> float:
    """ISK as people type it: '123,456,789', '123.4M', '1.2b', '500k ISK'.
    Raises ValueError for anything else."""
    cleaned = (text or "").strip().lower().removesuffix("isk")
    cleaned = cleaned.replace(",", "").replace(" ", "").replace("_", "")
    scale = 1.0
    if cleaned[-1:] in SUFFIXES:
        scale, cleaned = SUFFIXES[cleaned[-1:]], cleaned[:-1]
    value = float(cleaned) * scale
    if not math.isfinite(value):
        raise ValueError(text)
    return value


def per_hour(isk_amount: float, minutes: float) -> float | None:
    """ISK an hour, or None when no time is involved."""
    return isk_amount / minutes * 60 if minutes and minutes > 0 else None
