"""Numbers as the dashboard and explanations show them, so every page and
every reason writes ISK the same way."""

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


def per_hour(isk_amount: float, minutes: float) -> float | None:
    """ISK an hour, or None when no time is involved."""
    return isk_amount / minutes * 60 if minutes and minutes > 0 else None
