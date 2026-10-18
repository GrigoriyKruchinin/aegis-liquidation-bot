"""Formatting helpers for Telegram HTML templates. All dynamic values must pass `esc`."""

from __future__ import annotations

import html
from decimal import ROUND_HALF_UP, Decimal

SEPARATOR = "━━━━━━━━━━━━━━━━━━"


def esc(value: object) -> str:
    """HTML-escape any value for Telegram HTML parse mode."""
    return html.escape(str(value), quote=True)


def usd(value: Decimal) -> str:
    """Format a USD amount: ``$1,234.56`` (4 decimals below one dollar)."""
    places = Decimal("0.01") if abs(value) >= 1 else Decimal("0.0001")
    rounded = value.quantize(places, rounding=ROUND_HALF_UP)
    sign = "-" if rounded < 0 else ""
    return f"{sign}${abs(rounded):,}"


def amount(value: Decimal, max_places: int = 6) -> str:
    """Format a token amount with thousands separators and trimmed decimals."""
    places = 2 if abs(value) >= 1000 else max_places
    rounded = value.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)
    text = f"{rounded:,.{places}f}"
    return text.rstrip("0").rstrip(".") if "." in text else text


def ratio(value: Decimal, places: int = 4) -> str:
    """Format a ratio such as a health factor."""
    return f"{value.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)}"


def short_address(address: str) -> str:
    """``0x1234…abcd``."""
    return f"{address[:6]}…{address[-4:]}"


def compact_usd(value: Decimal) -> str:
    """Short USD amount: ``$950``, ``$12.3K``, ``$4.56M``, ``$1.2B``."""
    for threshold, suffix in ((Decimal(10**9), "B"), (Decimal(10**6), "M"), (Decimal(10**3), "K")):
        if abs(value) >= threshold:
            scaled = (value / threshold).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
            return f"${scaled}{suffix}"
    return f"${value.quantize(Decimal(1), rounding=ROUND_HALF_UP)}"


def duration(seconds: int) -> str:
    """Human duration: ``45s``, ``12m``, ``3h 20m``, ``2d 4h``."""
    if seconds < 60:
        return f"{seconds}s"
    minutes, _ = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h {minutes}m" if minutes else f"{hours}h"
    days, hours = divmod(hours, 24)
    return f"{days}d {hours}h" if hours else f"{days}d"
