"""Helper functions for Pacifica TP/SL (Create Position TP/SL).

This module converts normalized TradeDesk `set_tp` / `set_sl` requests into
Pacifica `POST /api/v1/positions/tpsl` payloads and signatures.

Normalization contract (input):

    {
      "operation": "set_tp" | "set_sl",
      "exchange": "pacifica",
      "account": "example",
      "symbol": "BTC",
      "side": "long" | "short",
      "price": 55000,          # 0 means remove TP/SL
      "position": { ... },     # normalized position
    }

We do not fetch positions here; TradeDesk / caller is responsible for
propagating the selected position snapshot.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Optional, Tuple

from .request_utils import _request_field


@dataclass
class PacificaTpslPayload:
    """Structured request to POST /api/v1/positions/tpsl.

    This is signer-agnostic: caller is responsible for attaching `account`,
    `signature`, and `timestamp` using the normal PacificaSigner.
    """

    symbol: str
    side: str  # "bid" or "ask"
    take_profit: Optional[dict]
    stop_loss: Optional[dict]

    def to_body(self, *, account: str, signature: str, timestamp: int, expiry_window_ms: int) -> dict:
        body: dict[str, Any] = {
            "account": account,
            "signature": signature,
            "timestamp": timestamp,
            "symbol": self.symbol,
            "side": self.side,
            "expiry_window": expiry_window_ms,
        }
        if self.take_profit is not None:
            body["take_profit"] = self.take_profit
        if self.stop_loss is not None:
            body["stop_loss"] = self.stop_loss
        return body


def _position_side_to_bid_ask(side: Any) -> str:
    """Return the Pacifica stop-order side needed to close the position.

    Pacifica positions report their position side (`bid` for long, `ask` for
    short), but `/positions/tpsl` expects the stop order side. A long is closed
    by an ask/sell stop order; a short is closed by a bid/buy stop order.
    """
    raw = str(side or "").lower()
    if raw in {"long", "buy", "bid", "b"}:
        return "ask"
    if raw in {"short", "sell", "ask", "a"}:
        return "bid"
    return raw or "ask"


def _decimal_string(value: Any) -> str:
    raw = str(value).strip()
    if not raw:
        return raw
    try:
        import math

        f = float(raw)
        if math.isfinite(f):
            txt = f"{f:.12f}".rstrip("0").rstrip(".")
            return txt or "0"
        return raw
    except Exception:
        return raw


def build_tpsl_payload(request: Mapping[str, Any]) -> Tuple[Optional[PacificaTpslPayload], Optional[str]]:
    """Return (payload, error) for a normalized `set_tp` / `set_sl` request.

    Does not sign or attach account; callers must provide those.

    Reads operation fields through the shared accessor so the call works
    whether the caller provided top-level fields (legacy direct callers)
    or routed through TradeDesk's normalize layer (which places the
    fields inside ``structured_request``).
    """
    op = str(_request_field(request, "operation") or "").lower()
    symbol = str(_request_field(request, "symbol") or "").upper()
    side = _position_side_to_bid_ask(_request_field(request, "side"))
    price = _request_field(request, "price")

    if not symbol:
        return None, "Missing symbol for Pacifica TP/SL"
    if price is None:
        return None, "Missing price for Pacifica TP/SL"

    price_str = _decimal_string(price)
    if price_str == "":
        return None, "Missing price for Pacifica TP/SL"

    # Pacifica Telegram UX contract: price=0 removes the corresponding leg.
    # Keep it as an explicit stop_price payload so the exchange, not the UI,
    # performs the removal.
    leg = {"stop_price": price_str}
    take_profit: Optional[dict] = None
    stop_loss: Optional[dict] = None

    if op == "set_tp":
        take_profit = leg
    elif op == "set_sl":
        stop_loss = leg
    else:
        return None, f"Unsupported TP/SL operation: {op}"

    return PacificaTpslPayload(symbol=symbol, side=side, take_profit=take_profit, stop_loss=stop_loss), None
