"""Telegram Trading Console wizard.

Plugin-local capability owned by plugins/platforms/telegram/trade_menu.
It owns Telegram UI/state and submits completed StructuredTradeRequests to
TradeDesk; it never calls an ExchangeAgent or exchange SDK directly.
"""

from .wizard import (
    handle_trade_callback,
    handle_trade_command,
    handle_trade_text,
    is_trade_command,
)

__all__ = [
    "handle_trade_callback",
    "handle_trade_command",
    "handle_trade_text",
    "is_trade_command",
]
