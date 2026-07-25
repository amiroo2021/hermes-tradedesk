"""Exchange router for TradeDesk."""
from __future__ import annotations

from typing import Any, Mapping


class TradeDeskRouter:
    """Route StructuredTradeRequest.exchange to an exchange-specific agent."""

    def __init__(self, *, hyperliquid_agent: Any, afx_agent: Any, pacifica_agent: Any, apex_agent: Any = None, lighter_agent: Any = None, rise_agent: Any = None, raydium_agent: Any = None) -> None:
        self.hyperliquid_agent = hyperliquid_agent
        self.afx_agent = afx_agent
        self.pacifica_agent = pacifica_agent
        self.apex_agent = apex_agent
        self.lighter_agent = lighter_agent
        self.rise_agent = rise_agent
        self.raydium_agent = raydium_agent

    def route(self, request: Mapping[str, Any]) -> Any:
        exchange = str(request.get("exchange") or "").lower()
        if exchange == "hyperliquid":
            return self.hyperliquid_agent
        if exchange == "afx":
            return self.afx_agent
        if exchange == "pacifica":
            return self.pacifica_agent
        if exchange == "apex":
            return self.apex_agent
        if exchange == "lighter":
            return self.lighter_agent
        if exchange == "rise":
            return self.rise_agent
        if exchange == "raydium":
            return self.raydium_agent
        return {
            "success": False,
            "exchange": exchange or None,
            "operation": request.get("operation"),
            "error": f"Unsupported exchange: {request.get('exchange')}",
            "structured_request": dict(request),
        }
