"""Shared presentation helpers for the Position Manager summary card.

Both ``trade_menu.wizard`` (the ``/trade → Position Manager`` flow)
and ``positions_menu.wizard`` (the ``/positions`` command) render
a per-position summary block before the keyboard. To avoid
duplicating the formatting logic across two files, this module
exposes the canonical implementation.

The mapping is:
  🔵 = Long
  🔴 = Short

TP and SL gracefully fall back to "—" when the normalized
position object does not carry the corresponding field (e.g.
exchanges whose ``positions`` endpoint does not enrich with
TP/SL data).

This module is *presentation only* — it has no side effects, does
not call TradeDesk or any exchange agent, and does not touch
wizard state. It accepts a normalized position dict and returns
formatted strings.
"""
from __future__ import annotations

from typing import Any


def fmt_number(value: Any) -> str:
    """Format a numeric value with thousands separators and trimming.

    Mirrors ``positions_menu.wizard._fmt_number`` exactly. Returns
    "None" for None / empty values (preserved for backward
    compatibility with downstream callers) and the raw string for
    unparseable non-numeric input.
    """
    try:
        return f"{float(value):,.8f}".rstrip("0").rstrip(".")
    except Exception:
        return "None" if value in (None, "") else str(value)


def fmt_pnl_compact(value: Any) -> str:
    """Format unrealized PnL to 2 decimals with a sign.

    Positive values get a leading ``+``. ``None`` or unparseable
    inputs fall back to ``"0.00"``. This matches the contract of
    ``positions_menu.wizard._fmt_pnl_compact``.
    """
    try:
        number = float(value)
    except Exception:
        return "0.00"
    sign = "+" if number >= 0 else ""
    return f"{sign}{number:.2f}"


def fmt_price_or_dash(value: Any) -> str:
    """Format a TP/SL price or show "—" when absent.

    ``None``, empty string, and non-numeric values all return the
    em-dash. Numeric values are routed through :func:`fmt_number`
    for consistent thousands-separator formatting.
    """
    if value in (None, ""):
        return "—"
    try:
        float(value)
    except Exception:
        return "—"
    return fmt_number(value)


def position_summary_card(position: dict) -> str:
    """Render a compact per-position summary card.

    The card shows, in order: emoji+symbol header, size, entry,
    unrealized PnL (signed, 2 decimals), TP, SL. TP/SL fall back
    to the em-dash when the normalized position does not carry
    them.

    The contract is identical to
    ``positions_menu.wizard._position_summary_card`` (Phase 41).
    """
    symbol = str(position.get("symbol") or "?").upper()
    side = str(position.get("side") or "").lower()
    emoji = "🔵" if side == "long" else "🔴"
    size = fmt_number(position.get("size"))
    entry = fmt_number(position.get("entry_price"))
    pnl = fmt_pnl_compact(position.get("unrealized_pnl"))
    tp = fmt_price_or_dash(position.get("take_profit"))
    sl = fmt_price_or_dash(position.get("stop_loss"))
    return "\n".join(
        [
            f"{emoji} {symbol}",
            f"Size: {size}",
            f"Entry: {entry}",
            f"PnL: {pnl}",
            f"TP: {tp}",
            f"SL: {sl}",
        ]
    )


def positions_screen_text(
    *,
    exchange: Any,
    account: Any,
    positions: list,
    error: Any = None,
) -> str:
    """Build the complete Position Manager header + summary text.

    Returns a string formatted like::

        💼 Positions — <exchange> / <account>

        Current Positions

        🔵 BTC
        Size: 0.05071
        Entry: 63,896.1
        PnL: +79.40
        TP: 70,000
        SL: 61,000

        🔴 HYPE
        Size: 9.8
        Entry: 67.3
        PnL: +52.70
        TP: —
        SL: —

        Select position:

    If ``error`` is provided, the summary block is replaced by
    the error message. If ``positions`` is empty, an empty-state
    marker is rendered.
    """
    exchange_label = str(exchange or "").strip()
    account_label = str(account or "").strip()
    if error:
        return (
            f"💼 Positions — {exchange_label} / {account_label}\n\n{error}"
        )
    if not positions:
        return (
            f"💼 Positions — {exchange_label} / {account_label}\n\n"
            "✅ No open positions."
        )
    header = (
        f"💼 Positions — {exchange_label} / {account_label}\n\n"
        "Current Positions\n\n"
    )
    summary = "\n\n".join(position_summary_card(pos) for pos in positions)
    return header + summary + "\n\nSelect position:"
