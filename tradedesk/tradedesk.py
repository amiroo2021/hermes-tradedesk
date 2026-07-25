"""TradeDesk public API.

TradeDesk is the single entry point between UI surfaces (Telegram /trade) and
exchange-specific agents. It converts StructuredTradeRequests into normalized
single-child, batch-child, or query/cancel requests before handing them to an
ExchangeAgent.
"""
from __future__ import annotations

import json
import logging
import math
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Mapping, Optional

from .afx_agent import AfxAgent
from .apex_agent import ApexAgent
from .hyperliquid_agent import HyperliquidAgent
from .lighter_agent import LighterAgent
from .pacifica_agent import PacificaAgent
from .rise_agent import RiseAgent
from .raydium_agent import RaydiumAgent
from .router import TradeDeskRouter

logger = logging.getLogger(__name__)


def _quantize_price_to_tick(price: Decimal, tick_size: Decimal) -> Decimal:
    """Quantize ``price`` to the nearest multiple of ``tick_size``.

    Decimal-safe: avoids binary-float modulo. Uses ROUND_HALF_UP semantics
    so the price ends up on the nearest tick boundary; ``tick_size`` is
    assumed positive. The function preserves ``price`` exactly when ``price``
    already lies on a tick boundary (i.e. ``price % tick_size == 0``).
    """
    if tick_size <= 0:
        return price
    n_ticks = (price / tick_size).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return n_ticks * tick_size


class TradeDesk:
    """Central router/orchestrator for StructuredTradeRequests."""

    def __init__(self, hyperliquid_agent: Optional[Any] = None, afx_agent: Optional[Any] = None, pacifica_agent: Optional[Any] = None, apex_agent: Optional[Any] = None, lighter_agent: Optional[Any] = None, rise_agent: Optional[Any] = None, raydium_agent: Optional[Any] = None) -> None:
        self.router = TradeDeskRouter(
            hyperliquid_agent=hyperliquid_agent or HyperliquidAgent(),
            afx_agent=afx_agent or AfxAgent(),
            pacifica_agent=pacifica_agent or PacificaAgent(),
            apex_agent=apex_agent or ApexAgent(),
            lighter_agent=lighter_agent or LighterAgent(),
            rise_agent=rise_agent or RiseAgent(),
            raydium_agent=raydium_agent or RaydiumAgent(),
        )

    def _pacifica_agent(self):
        """Return the configured PacificaAgent (if any) via the router."""
        return getattr(self.router, "pacifica_agent", None)

    def _child_order(
        self,
        request,
        *,
        child_id,
        price=None,
        size=None,
        order_type=None,
    ):
        """Build a single normalized child-order dict.

        Used by both ``_normalize_place_order`` and ``_normalize_ladder``.
        Numeric fields are coerced to ``float`` for downstream consumer
        compatibility (the Pacifica fix is responsible for ensuring
        these values are quantized to the exchange's authoritative
        lot_size / tick_size *before* the child dict is built).
        """
        resolved_order_type = str(
            order_type or request.get("order_type") or ""
        ).lower()
        side = str(request.get("side") or "").lower()
        child = {
            "child_id": int(child_id),
            "symbol": str(request.get("symbol") or "").upper(),
            "side": side,
            "is_buy": side == "buy",
            "order_type": resolved_order_type,
            "size": float(size if size is not None else request.get("size")),
            "reduce_only": bool(request.get("reduce_only", False)),
        }
        if resolved_order_type == "limit":
            child["price"] = float(
                price if price is not None else request.get("price")
            )
        return child

    def list_accounts(self, exchange: str) -> dict:
        """Return account names with complete trading credentials for an exchange.

        TradeDesk asks the selected ExchangeAgent; values come from process env
        and the active Hermes .env. Secret values are never returned.
        """
        request = {"exchange": exchange, "operation": "list_accounts"}
        agent_or_result = self.router.route(request)
        if isinstance(agent_or_result, dict):
            return {"success": False, "exchange": exchange, "accounts": [], "error": agent_or_result.get("error"), "message": agent_or_result.get("error")}
        if not hasattr(agent_or_result, "list_accounts"):
            return {"success": False, "exchange": exchange, "accounts": [], "error": f"{exchange} agent does not support account discovery", "message": f"{exchange} agent does not support account discovery"}
        result = agent_or_result.list_accounts()
        if not isinstance(result, dict):
            return {"success": False, "exchange": exchange, "accounts": [], "error": "Invalid account discovery result", "message": "Invalid account discovery result"}
        result.setdefault("exchange", str(exchange).lower())
        result.setdefault("accounts", [])
        return result

    def execute(self, request: Mapping[str, Any]) -> dict:
        """Normalize a StructuredTradeRequest and execute it through an ExchangeAgent."""
        if not isinstance(request, Mapping):
            return self._execution_result(
                False,
                "❌ Invalid trade request: StructuredTradeRequest must be a mapping.",
                {"error": "StructuredTradeRequest must be a mapping"},
            )

        agent_or_result = self.router.route(request)
        if isinstance(agent_or_result, dict):
            return self._wrap_agent_result(request, agent_or_result)

        normalized_or_error = self.normalize(request)
        if normalized_or_error.get("success") is False:
            return self._wrap_agent_result(request, normalized_or_error)
        raw_result = agent_or_result.execute(normalized_or_error)
        return self._wrap_agent_result(request, raw_result)

    def normalize(self, request: Mapping[str, Any]) -> dict:
        """Turn a StructuredTradeRequest into normalized work for an ExchangeAgent."""
        operation = str(request.get("operation") or "")
        try:
            if operation == "place_order":
                return self._normalize_place_order(request)
            if operation == "ladder":
                return self._normalize_ladder(request)
            if operation == "cancel_orders":
                return self._normalize_passthrough(request, "cancel_orders")
            if operation == "balance":
                return self._normalize_passthrough(request, "balance")
            if operation == "open_orders":
                return self._normalize_passthrough(request, "open_orders")
            if operation == "order":
                return self._normalize_passthrough(request, "order")
            if operation == "cancel_order":
                return self._normalize_passthrough(request, "cancel_order")
            if operation in {"positions", "set_tp", "set_sl"}:
                return self._normalize_passthrough(request, operation)
        except Exception as exc:
            return self._error(request, str(exc), exc.__class__.__name__)
        return self._error(request, f"Unsupported operation: {operation}")

    def _normalize_place_order(self, request: Mapping[str, Any]) -> dict:
        child = self._child_order(request, child_id=1)
        return {
            "version": 1,
            "exchange": self._exchange(request),
            "account": request.get("account"),
            "operation": "order",
            "parent_operation": "place_order",
            "structured_request": dict(request),
            "child_order": child,
            "child_orders": [child],
        }

    def _normalize_ladder(self, request: Mapping[str, Any]) -> dict:
        order_count = int(request.get("order_count"))
        total_volume = float(request.get("total_volume"))
        start_price = float(request.get("start_price"))
        end_price = float(request.get("end_price"))
        distribution = str(request.get("distribution") or "uniform").lower()
        if order_count <= 0:
            raise ValueError("order_count must be positive")

        if order_count == 1:
            prices = [start_price]
        else:
            prices = [start_price + (end_price - start_price) * i / (order_count - 1) for i in range(order_count)]

        if distribution == "half_gaussian":
            sigma = 0.45
            denominator = max(order_count - 1, 1)
            weights = [
                math.exp(-0.5 * (((1 - (i / denominator)) / sigma) ** 2))
                for i in range(order_count)
            ]
            total_weight = sum(weights)
            sizes = [weight / total_weight * total_volume for weight in weights]
        else:
            sizes = [total_volume / order_count for _ in range(order_count)]

        # ------------------------------------------------------------------
        # Ladder child normalization (isolated, exchange-specific).
        #
        # Quantize every child size to the symbol's authoritative lot_size
        # and every child price to the symbol's authoritative tick_size
        # before validation or signing. Decimal-safe arithmetic; no binary
        # float modulo. Preserves direction (sell: ascending, buy:
        # descending), preserves the half-Gaussian shape as closely as
        # possible, drops zero-amount children, reconciles total volume by
        # distributing leftover lot-units deterministically, and merges
        # duplicate prices deterministically.
        # ------------------------------------------------------------------
        exchange = self._exchange(request)
        symbol = str(request.get("symbol") or "").upper()
        side = str(request.get("side") or "").lower()
        tick_size, lot_size, lot_fallback_used, tick_fallback_used = (
            self._resolve_ladder_step_sizes(exchange, symbol)
        )
        normalized_children, ladder_meta = self._quantize_ladder_children(
            prices=prices,
            raw_sizes=sizes,
            side=side,
            tick_size=tick_size,
            lot_size=lot_size,
            lot_fallback_used=lot_fallback_used,
            tick_fallback_used=tick_fallback_used,
            request=request,
        )

        child_orders = []
        for index, child in enumerate(normalized_children, start=1):
            child_orders.append(
                self._child_order(
                    request,
                    child_id=index,
                    price=child["price"],
                    size=child["size"],
                    order_type="limit",
                )
            )

        return {
            "version": 1,
            "exchange": self._exchange(request),
            "account": request.get("account"),
            "operation": "batch_orders",
            "parent_operation": "ladder",
            "distribution": distribution,
            "structured_request": dict(request),
            "child_orders": child_orders,
            "ladder_normalization": ladder_meta,
        }

    def _normalize_passthrough(
        self, request: Mapping[str, Any], operation: str
    ) -> dict:
        """Return a normalized pass-through dict for a no-transformation operation.

        Used by ``normalize()`` for operations that TradeDesk does not have
        to mutate before handing off to the agent (read-only queries and
        passthrough operations):

          * balance / positions / open_orders — read-only.
          * cancel_orders / cancel_order / cancel_symbol — the agent owns
            the cancellation logic and only requires the original account,
            symbol, and order_id set from the request.
          * set_tp / set_sl — the TP/SL builder on the agent path owns the
            request → payload transformation; TradeDesk only needs to keep
            the request structured and exchange-tagged.

        Contract (the agent path consumes the returned dict via
        ``agent.execute(normalized_or_error)`` and the message renderer
        consumes ``raw_result`` via ``_message_for_result(request,
        raw_result)``):

          * ``version`` is always 1.
          * ``exchange`` is the lowercased exchange name from the request.
          * ``account`` is passed through from the request.
          * ``operation`` is the explicit operation argument (e.g. ``"balance"``).
          * ``parent_operation`` mirrors the explicit operation argument so
            ``_message_for_result`` chooses the right renderer.
          * ``structured_request`` is a shallow copy of the original request
            so the renderer can re-read ``request.get("account")`` etc.

        Behavior guarantees:

          * Never mutates the input ``request``.
          * Returns a clear, normalized error dict on a malformed input
            (a non-Mapping request, a missing/empty ``operation``, etc.)
            instead of raising. The dict shape matches
            ``_normalize_place_order`` and ``_normalize_ladder`` outputs as
            closely as possible.
          * Exchange-neutral: contains no Pacifica-, Hyperliquid-, AFX-, or
            Apex-specific branches. The agent layer applies exchange-specific
            validation, rounding, and signing.
        """
        if not isinstance(request, Mapping):
            return {
                "success": False,
                "exchange": None,
                "operation": operation,
                "parent_operation": operation,
                "error": (
                    "TradeDesk received an invalid request: StructuredTradeRequest "
                    "must be a mapping."
                ),
                "error_type": "InvalidRequest",
                "structured_request": {},
            }
        op = (operation or "").strip()
        if not op:
            return {
                "success": False,
                "exchange": str(request.get("exchange") or "").lower() or None,
                "operation": operation,
                "parent_operation": operation,
                "error": "TradeDesk received an empty operation",
                "error_type": "MissingOperation",
                "structured_request": dict(request),
            }
        return {
            "version": 1,
            "exchange": str(request.get("exchange") or "").lower(),
            "account": request.get("account"),
            "operation": op,
            "parent_operation": op,
            "structured_request": dict(request),
        }

    def _resolve_ladder_step_sizes(
        self, exchange: str, symbol: str
    ) -> tuple[Decimal, Decimal, bool, bool]:
        """Resolve authoritative lot_size and tick_size for a ladder.

        For Pacifica, ``PacificaAgent._ensure_market_info()`` populates the
        lot/tick maps from ``GET /api/v1/info``. Other exchanges fall back
        to the existing decimal-precision helpers.

        Returns ``(tick_size, lot_size, lot_fallback_used, tick_fallback_used)``.
        ``fallback_used`` is True when the authoritative metadata source had
        no entry for the symbol and we used the agent's fallback constant.
        """
        if exchange == "pacifica":
            agent = self._pacifica_agent()
            try:
                if agent is not None:
                    agent._ensure_market_info()
                    tick = agent._tick_size_by_symbol.get(symbol.upper())
                    lot = agent._lot_size_by_symbol.get(symbol.upper())
                    tick_fb = tick is None
                    lot_fb = lot is None
                    tick = tick if tick is not None else agent._tick_size_fallback
                    lot = lot if lot is not None else agent._lot_size_fallback
                    return (
                        Decimal(str(tick)),
                        Decimal(str(lot)),
                        bool(lot_fb),
                        bool(tick_fb),
                    )
            except Exception:
                pass
            # Fall through to safe fallbacks if metadata is unavailable.
            return (Decimal("0.00000001"), Decimal("0.00000001"), True, True)
        # Other exchanges: not in scope for this Pacifica fix; return
        # the existing decimal fallbacks so the helper stays safe if the
        # function is called outside the Pacifica ladder path.
        return (Decimal("0.00000001"), Decimal("0.00000001"), True, True)

    def _quantize_ladder_children(
        self,
        prices,
        raw_sizes,
        side,
        tick_size,
        lot_size,
        lot_fallback_used,
        tick_fallback_used,
        request,
    ):
        """Quantize pre-computed ladder children to lot_size / tick_size.

        ``prices`` and ``raw_sizes`` are the unrounded inputs from
        ``_normalize_ladder`` (uniform or half-Gaussian, whichever the
        caller computed). The function only normalizes precision —
        it does not re-derive the distribution.

        Returns ``(normalized_children, meta)`` where each normalized child
        is a dict ``{price: Decimal, size: Decimal, original_indices: [ids]}``.
        ``meta`` carries diagnostics for the structured result.
        """
        n = len(prices)
        side_lower = side.lower()

        # Only quantize when both lot and tick came from the authoritative
        # exchange metadata (i.e. the Pacifica ``/api/v1/info`` source).
        # For other exchanges (Hyperliquid, AFX, apex) the existing
        # exchange-specific agents apply their own price/size handling
        # downstream, and the legacy tests assert exact float equality.
        pacifica_quantize = not (lot_fallback_used and tick_fallback_used)

        # 1) Quantize prices to tick_size (nearest), preserving direction.
        if pacifica_quantize:
            quantized_prices = []
            for raw_price in prices:
                qp = _quantize_price_to_tick(Decimal(str(raw_price)), tick_size)
                quantized_prices.append(qp)
        else:
            # Non-Pacifica path: preserve raw float-derived prices.
            quantized_prices = [Decimal(str(p)) for p in prices]

        # 2) Cast raw sizes to Decimal.
        raw_decimal_sizes = [Decimal(str(s)) for s in raw_sizes]
        total_volume = Decimal(str(request.get("total_volume")))

        # 3) Floor each size to lot_size.
        if pacifica_quantize:
            quantized_sizes = []
            for s in raw_decimal_sizes:
                qty = (s // lot_size) * lot_size
                if qty < 0:
                    qty = Decimal(0)
                quantized_sizes.append(qty)
        else:
            # Non-Pacifica path: preserve raw sizes.
            quantized_sizes = list(raw_decimal_sizes)

        # 4) Distribute any remaining valid lot units to children with the
        #    largest unquantized size first (preserves the half-Gaussian
        #    shape — heavy-tail children get priority for the rounding-up).
        requested_total = total_volume
        if pacifica_quantize:
            rounded_total = sum(quantized_sizes, Decimal(0))
            deficit = requested_total - rounded_total
            # Cap deficit at lot_size * n to avoid over-allocating beyond
            # the requested total volume.
            max_remainder = lot_size * Decimal(n)
            if deficit > max_remainder:
                deficit = max_remainder
            if deficit < 0:
                deficit = Decimal(0)
            unit = lot_size
            n_units = int((deficit / unit).to_integral_value()) if unit > 0 else 0
            if n_units > 0:
                order_for_topup = sorted(
                    range(n),
                    key=lambda i: (-raw_decimal_sizes[i], i),
                )
                for idx in order_for_topup[:n_units]:
                    quantized_sizes[idx] += unit
        # Non-Pacifica path: no topup.

        # 5) Drop children whose quantized size is 0.
        surviving_indices = [i for i in range(n) if quantized_sizes[i] > 0]

        # 6) Detect duplicate prices (caused by tick rounding) and merge
        #    deterministically: combine sizes, preserve original_indices.
        survivors = []
        for i in surviving_indices:
            survivors.append({
                "price": quantized_prices[i],
                "size": quantized_sizes[i],
                "original_indices": [i + 1],  # 1-based child_id
            })
        merged = []
        for s in survivors:
            attached = False
            for m in merged:
                if m["price"] == s["price"]:
                    m["size"] += s["size"]
                    m["original_indices"] += s["original_indices"]
                    attached = True
                    break
            if not attached:
                merged.append({**s})

        # 7) Preserve direction (sell: ascending price; buy: descending).
        if side_lower == "buy":
            merged.sort(key=lambda x: x["price"], reverse=True)
        else:
            merged.sort(key=lambda x: x["price"])

        # 8) Reconcile volume.
        submitted_total = sum((m["size"] for m in merged), Decimal(0))
        dropped_count = n - len(surviving_indices)
        merged_count = len(survivors) - len(merged)

        meta = {
            "exchange": str(request.get("exchange") or ""),
            "symbol": str(request.get("symbol") or "").upper(),
            "side": side_lower,
            "order_count_requested": n,
            "order_count_survived": len(merged),
            "dropped_due_to_lot_quantization": dropped_count,
            "merged_due_to_tick_quantization": merged_count,
            "tick_size": str(tick_size),
            "lot_size": str(lot_size),
            "tick_fallback_used": bool(tick_fallback_used),
            "lot_fallback_used": bool(lot_fallback_used),
            "requested_total_volume": str(requested_total),
            "submitted_total_size": str(submitted_total),
            "unrounded_first_size": str(raw_decimal_sizes[0]) if raw_decimal_sizes else None,
            "unrounded_last_size": str(raw_decimal_sizes[-1]) if raw_decimal_sizes else None,
        }
        return merged, meta

    @staticmethod
    def _money(value: Any) -> str:
        try:
            return f"${float(value):,.2f}"
        except Exception:
            return "$0.00" if value in (None, "") else f"${value}"

    @staticmethod
    def _number(value: Any) -> str:
        try:
            return f"{float(value):,.2f}".rstrip("0").rstrip(".")
        except Exception:
            return str(value)

    @staticmethod
    def _price(value: Any) -> str:
        try:
            formatted = f"{float(value):,.6f}".rstrip("0").rstrip(".")
            if "." not in formatted:
                formatted += ".00"
            elif len(formatted.rsplit(".", 1)[1]) == 1:
                formatted += "0"
            return f"${formatted}"
        except Exception:
            return "$0.00" if value in (None, "") else f"${value}"

    @staticmethod
    def _execution_result(success: bool, message: str, data: Any) -> dict:
        return {"success": bool(success), "message": message, "data": data}

    def _wrap_agent_result(self, request: Mapping[str, Any], raw_result: Any) -> dict:
        """Return Telegram-safe ExecutionResult while logging raw details."""
        logger.info("TradeDesk raw execution result: %s", json.dumps(raw_result, default=str, ensure_ascii=False))
        success = bool(raw_result.get("success")) if isinstance(raw_result, Mapping) else False
        message = self._message_for_result(request, raw_result)
        return self._execution_result(success, message, raw_result)

    @staticmethod
    def _exchange_label(request: Mapping[str, Any], raw_result: Mapping[str, Any] | None = None) -> str:
        raw_exchange = raw_result.get("exchange") if isinstance(raw_result, Mapping) else None
        exchange = str(raw_exchange or request.get("exchange") or "Exchange").strip()
        labels = {"afx": "AFX", "hyperliquid": "Hyperliquid", "pacifica": "Pacifica"}
        return labels.get(exchange.lower(), exchange.title() if exchange else "Exchange")

    @staticmethod
    def _extract_result_error(raw_result: Mapping[str, Any]) -> Optional[str]:
        for key in ("exchange_response", "raw_response"):
            raw = raw_result.get(key)
            if isinstance(raw, Mapping):
                parts = [raw.get("message"), raw.get("error")]
                data = raw.get("data")
                if isinstance(data, Mapping):
                    parts.extend([data.get("txMsg"), data.get("error"), data.get("reason")])
                message = "; ".join(str(part) for part in parts if part not in (None, "", "success"))
                if message:
                    return message
        return None

    def _message_for_result(self, request: Mapping[str, Any], raw_result: Any) -> str:
        if not isinstance(raw_result, Mapping):
            return "❌ Trade request failed: invalid execution result."
        exchange_label = self._exchange_label(request, raw_result)
        operation = str(raw_result.get("parent_operation") or raw_result.get("operation") or request.get("operation") or "")
        # The chunked Hyperliquid cancel path returns its own structured
        # success=false/partial/failure messages. Always route through the
        # dedicated cancel renderer so users never see the misleading
        # "❌ cancel_orders failed" generic error for a verified partial.
        if (
            operation == "cancel_orders"
            and "verified_success" in raw_result
        ):
            return self._format_cancel_orders_message(request, raw_result, exchange_label)
        # Phase 38: set_tp / set_sl uses the normalized
        # ``verification_status`` field produced by the bounded
        # ``/api/v1/tx`` verifier in the lighter_agent. The renderer
        # does NOT interpret exchange-specific logic; it only maps
        # the status string to a Telegram-safe message. This branch
        # runs BEFORE the generic success=False fallback so we
        # surface a status-specific message even when the envelope
        # reports ``success=False`` because verification failed.
        if operation in {"set_tp", "set_sl"}:
            vs = raw_result.get("verification_status")
            if vs == "confirmed_resting":
                return (
                    f"✅ {exchange_label} {operation} live — verified"
                )
            if vs == "confirmed_transaction":
                return (
                    f"✅ {exchange_label} {operation} accepted by exchange"
                )
            if vs == "confirmed_queued":
                return (
                    f"⏳ {exchange_label} {operation} queued"
                )
            if vs == "confirmed_rejected":
                return (
                    f"❌ {exchange_label} {operation} rejected by exchange"
                )
            if vs == "confirmed_sequenced":
                # Neutral message: the Lighter documentation does
                # not define whether this state corresponds to a
                # resting TP/SL, a queued-but-not-yet-applied tx,
                # or an eventually-discarded tx. We surface the
                # observation only; the operator decides the next
                # step.
                return (
                    f"⏳ {exchange_label} {operation} sequenced at the exchange."
                    f"\n\nThe exchange documentation does not define whether this state results in a resting TP/SL order."
                )
            if vs == "unconfirmed_at_exchange":
                return (
                    f"❌ {exchange_label} {operation} submitted but the exchange did not confirm the transaction"
                )
            if vs == "verification_timeout":
                return (
                    f"⏳ {exchange_label} {operation} verification timed out"
                )
            if vs == "verification_error":
                return (
                    f"❌ {exchange_label} {operation} verification failed"
                )
            # No verification_status: fall through to legacy
            # success/failure handling below.
        if not raw_result.get("success") and operation not in {"ladder", "batch_orders"}:
            error = raw_result.get("error") or raw_result.get("traceback") or self._extract_result_error(raw_result) or "Unknown error"
            return f"❌ {exchange_label} {request.get('operation')} failed\n\n{error}"
        if operation == "balance":
            return self._format_balance_message(request, raw_result, exchange_label)
        if operation in {"place_order", "order"}:
            child_results = raw_result.get("child_results") or []
            child = child_results[0] if child_results else {}
            child_order = child.get("child_order") if isinstance(child, Mapping) else None
            if isinstance(child_order, Mapping):
                child = {**child_order, **child}
            return (
                f"✅ {exchange_label} order submitted — {request.get('account')}\n\n"
                f"{child.get('symbol') or request.get('symbol')} {str(child.get('side') or request.get('side') or '').title()} "
                f"{child.get('size') if isinstance(child.get('size'), str) else self._number(child.get('size') or request.get('size'))}\n"
                f"Type: {str(child.get('order_type') or request.get('order_type') or '').title()}"
                + (f"\nPrice: {self._money(child.get('price') or request.get('price'))}" if (child.get('price') or request.get('price')) is not None else "")
            )
        if operation in {"ladder", "batch_orders"}:
            # Resolve the per-child list from any of the known envelope
            # keys (defensive read; preserves exchange-agnostic contract):
            #   - Hyperliquid chunked: ``child_results``
            #   - Lighter (and any future adapter): ``children``
            #   - Lighter structured_request: ``structured_request.child_orders``
            #   - Pacifica / pre-fix ladder: ``normalized_request.child_orders``
            child_results = raw_result.get("child_results") or []
            if not child_results:
                child_results = (
                    raw_result.get("children")
                    or raw_result.get("structured_request", {}).get("child_orders")
                    or raw_result.get("normalized_request", {}).get("child_orders")
                    or []
                )
            count = len(child_results)
            mode = raw_result.get("submission_mode")
            mode_line = "\nMode: one-by-one" if mode == "one_by_one" else ""
            # Chunked Hyperliquid placement results carry an explicit
            # ``submission_mode="chunked"`` plus ``verified_success`` /
            # ``partial_success`` / ``verification_mismatch`` flags. Use
            # those for an unambiguous rendered message and only fall
            # back to the legacy happy/sad branches when those flags
            # are absent (older exchange paths).
            if mode == "chunked" and "verified_success" in raw_result:
                return self._format_ladder_chunked_message(
                    request, raw_result, exchange_label, child_results, count,
                    mode_line,
                )
            if not raw_result.get("success"):
                failed = [child for child in child_results if isinstance(child, Mapping) and not child.get("success")]
                reason = raw_result.get("error") or "One or more child orders failed"
                details = []
                for child in failed[:5]:
                    raw_verification = child.get("verification")
                    verification = raw_verification if isinstance(raw_verification, Mapping) else {}
                    raw_sdk_payload = child.get("sdk_payload")
                    sdk_payload = raw_sdk_payload if isinstance(raw_sdk_payload, Mapping) else {}
                    shown_size = sdk_payload.get("qty") or child.get("size")
                    shown_price = sdk_payload.get("px") or child.get("price")
                    details.append(
                        f"• #{child.get('child_id')} {child.get('symbol')} {self._number(shown_size)} @ {self._price(shown_price)}: "
                        f"{child.get('error') or verification.get('reason') or 'not verified'}"
                    )
                suffix = ("\n" + "\n".join(details)) if details else ""
                # Distinguish exchange acceptance vs verified-resting in
                # the failure path as well — the operator needs to see
                # how many POSTs the exchange actually accepted before
                # the failure. We read from the canonical counters when
                # present, falling back to the per-child list length.
                accepted_count = (
                    raw_result.get("exchange_accepted_count")
                    if isinstance(raw_result.get("exchange_accepted_count"), int)
                    else sum(1 for c in child_results if isinstance(c, Mapping) and c.get("success"))
                )
                return f"❌ {exchange_label} ladder not confirmed — {request.get('account')}\n\nChild orders: {count}{mode_line}\nAccepted by exchange: {accepted_count}\n{reason}{suffix}"
            # Success path: distinguish exchange acceptance from
            # verified-resting so the operator sees both. Exchange
            # acceptance is the authoritative signal that the orders
            # were created; the bounded post-read is a confirmation
            # step that may lag the matching engine.
            accepted_count = (
                raw_result.get("exchange_accepted_count")
                if isinstance(raw_result.get("exchange_accepted_count"), int)
                else count
            )
            verified_count = (
                raw_result.get("verified_open_count")
                if isinstance(raw_result.get("verified_open_count"), int)
                else raw_result.get("verified_resting_count")
                if isinstance(raw_result.get("verified_resting_count"), int)
                else None
            )
            if verified_count is None or verified_count >= accepted_count:
                # Either no verification info is present, or every
                # accepted child has been verified resting — render the
                # compact happy-path message.
                return f"✅ {exchange_label} ladder submitted — {request.get('account')}\n\nChild orders: {count}{mode_line}"
            # Verification has not yet caught up to acceptance.
            # Surface both numbers explicitly so the operator knows
            # the children are accepted but propagation is pending.
            await_count = accepted_count - verified_count
            return (
                f"✅ {exchange_label} ladder submitted — {request.get('account')}\n\n"
                f"Accepted by exchange: {accepted_count}/{count}\n"
                f"Verified resting: {verified_count}/{count}\n"
                f"Awaiting propagation: {await_count}{mode_line}"
            )
        if operation == "cancel_orders":
            return self._format_cancel_orders_message(request, raw_result, exchange_label)
        if operation == "open_orders":
            return self._format_open_orders_message(request, raw_result, exchange_label)
        if operation == "positions":
            positions = raw_result.get("positions") or []
            return f"💼 {exchange_label} Positions — {request.get('account')}\n\nOpen positions: {len(positions) if isinstance(positions, list) else 0}"
        if operation in {"set_tp", "set_sl"}:
            # Legacy fallback (older envelopes without
            # ``verification_status``): preserve the message-or-fallback
            # contract. The Phase 38 verification_status branch above
            # handles the post-Phase-38 envelopes.
            return raw_result.get("message") or f"✅ {exchange_label} {operation} complete — {request.get('account')}"
        return f"✅ {exchange_label} {operation or 'request'} complete — {request.get('account')}"

    @staticmethod
    def _compute_open_orders_rich_summary(orders: list) -> list[dict]:
        """Compute the per-(symbol, side) rich summary used by the
        exchange-agnostic Open Orders view.

        Inputs are normalized TradeDesk order objects — the canonical
        ``orders`` list returned by every exchange's
        ``_open_orders`` dispatcher. The shape of those orders is not
        unified across exchanges today (e.g. Lighter carries
        ``remaining_base_amount``; Hyperliquid uses ``size``; Pacifica
        uses ``size = initial_amount``; AFX orders carry no price or
        size at all). This helper reads whatever fields are present,
        using safe fallbacks, so it works for every exchange
        automatically.

        The function is a pure presentation-layer computation:
        it does not change request/response contracts, normalization,
        or retrieval.

        Output: list of dicts with shape::

            {
                "symbol":   "BTC",
                "side":     "buy" | "sell",
                "count":    <int>,
                "total":    <Decimal string or None>,
                "min_price": <Decimal string or None>,
                "max_price": <Decimal string or None>,
            }

        Sort order:
          1. symbol (lexicographic, upper-cased)
          2. side (buy before sell when both exist for a symbol)
        """
        from decimal import Decimal, InvalidOperation

        def _coerce_decimal(value: Any) -> Optional[Decimal]:
            if value is None or value == "":
                return None
            try:
                d = Decimal(str(value))
            except (InvalidOperation, ValueError, TypeError):
                return None
            if not d.is_finite():
                return None
            return d

        def _format_decimal(d: Optional[Decimal], precision: Optional[int] = None) -> Optional[str]:
            if d is None:
                return None
            if precision is not None:
                # Use a fixed-precision quantize for fields like VWAP
                # where the user expects consistent decimal places.
                try:
                    q = Decimal(10) ** -precision
                    rounded = d.quantize(q)
                except (InvalidOperation, ValueError):
                    rounded = d
                return format(rounded, "f")
            try:
                normalized = d.normalize()
            except Exception:
                return str(d)
            if normalized == normalized.to_integral_value():
                return format(normalized, "f")
            return format(normalized, "f").rstrip("0").rstrip(".")

        def _read_side(order: Mapping[str, Any]) -> Optional[str]:
            """Return the canonical lowercase side or None if unparseable."""
            raw = order.get("side")
            if raw is None:
                return None
            s = str(raw).strip().lower()
            if s in {"buy", "b", "bid", "long"}:
                return "buy"
            if s in {"sell", "s", "ask", "short"}:
                return "sell"
            return None

        def _read_symbol(order: Mapping[str, Any]) -> str:
            return str(
                order.get("symbol")
                or order.get("coin")
                or order.get("display_symbol")
                or order.get("exchange_symbol")
                or ""
            ).upper().strip()

        def _read_price(order: Mapping[str, Any]) -> Optional[Decimal]:
            # The rich summary reports the price RANGE of resting
            # orders, not the trigger prices of conditional orders.
            # We therefore read the regular order-price field
            # variants and explicitly skip trigger_* — those are
            # present on conditional orders (e.g. AFX TP/SL) and
            # do not describe where the order would rest.
            for key in (
                "price",
                "limitPx", "limit_px",
                "px",
            ):
                if key in order:
                    d = _coerce_decimal(order.get(key))
                    if d is not None and d > 0:
                        return d
            return None

        def _read_remaining_qty(order: Mapping[str, Any]) -> Optional[Decimal]:
            """The unfilled quantity of the order.

            Priority: ``remaining_base_amount`` (Lighter) > ``size``
            (Hyperliquid) > ``quantity`` (Orderly/Raydium — the unfilled
            amount for an INCOMPLETE order) > ``visible_quantity``
            (Orderly/Raydium alias for the same) > ``initial_base_amount``
            (Pacifica-style full amount) > ``initial_amount``. If the
            order has ``filled_*`` fields and no explicit remaining
            field, the remaining qty is computed as initial − filled.
            """
            for key in (
                "remaining_base_amount",
                "remaining_amount",
                "remaining_qty",
                "remaining_size",
                "quantity",          # Orderly/Raydium: quantity IS the unfilled
                "visible_quantity",  # Orderly/Raydium visible_quantity = unfilled for LIMIT
            ):
                if key in order:
                    d = _coerce_decimal(order.get(key))
                    if d is not None and d >= 0:
                        return d
            for key in ("size",):
                if key in order:
                    d = _coerce_decimal(order.get(key))
                    if d is not None and d >= 0:
                        return d
            initial = None
            for key in ("initial_base_amount", "initial_amount"):
                if key in order:
                    initial = _coerce_decimal(order.get(key))
                    if initial is not None:
                        break
            if initial is not None:
                filled = Decimal(0)
                for key in ("filled_base_amount", "filled_amount",
                            "filled_size", "filled_qty"):
                    if key in order:
                        d = _coerce_decimal(order.get(key))
                        if d is not None:
                            filled = filled + d
                remaining = initial - filled
                return remaining if remaining > 0 else None
            return None

        # Skip orders that are explicitly inactive. Some exchanges
        # (e.g. AFX) pre-filter inactive orders out of ``orders``;
        # others (e.g. Lighter) include a ``status`` field. Treat
        # ``is_active`` as authoritative when present, and only
        # reject orders whose ``is_active`` is explicitly False.
        def _is_active(order: Mapping[str, Any]) -> bool:
            if "is_active" in order:
                return bool(order.get("is_active"))
            return True

        # Group by (symbol, side). Skip orders with no symbol or
        # unparseable side — they cannot be summarized.
        buckets: dict[tuple[str, str], dict] = {}
        for order in orders:
            if not isinstance(order, Mapping):
                continue
            if not _is_active(order):
                continue
            symbol = _read_symbol(order)
            side = _read_side(order)
            if not symbol or side is None:
                continue
            key = (symbol, side)
            bucket = buckets.get(key)
            if bucket is None:
                bucket = {
                    "symbol": symbol,
                    "side": side,
                    "count": 0,
                    "_total": Decimal(0),
                    "_has_total": False,
                    "_sum_price_x_qty": Decimal(0),
                    "_has_vwap": False,
                    "_min_price": None,
                    "_max_price": None,
                }
                buckets[key] = bucket
            bucket["count"] += 1
            qty = _read_remaining_qty(order)
            if qty is not None:
                bucket["_total"] = bucket["_total"] + qty
                bucket["_has_total"] = True
            price = _read_price(order)
            if price is not None:
                if bucket["_min_price"] is None or price < bucket["_min_price"]:
                    bucket["_min_price"] = price
                if bucket["_max_price"] is None or price > bucket["_max_price"]:
                    bucket["_max_price"] = price
                # Accumulate for VWAP only when both price and qty
                # are present for this order.
                if qty is not None:
                    bucket["_sum_price_x_qty"] = (
                        bucket["_sum_price_x_qty"] + price * qty
                    )
                    bucket["_has_vwap"] = True

        # Materialize to output shape.
        result: list[dict] = []
        for bucket in buckets.values():
            total_dec = bucket["_total"] if bucket["_has_total"] else None
            vwap_dec: Optional[Decimal] = None
            if bucket.get("_has_vwap") and total_dec is not None and total_dec > 0:
                raw_vwap = bucket["_sum_price_x_qty"] / total_dec
                # Round VWAP to 2 decimal places — standard financial
                # precision. The raw value can have 20+ decimal places
                # because Decimal division doesn't truncate by default.
                vwap_dec = raw_vwap.quantize(Decimal("0.01"))
            total_str = (
                _format_decimal(total_dec) if total_dec is not None else None
            )
            result.append({
                "symbol": bucket["symbol"],
                "side": bucket["side"],
                "count": bucket["count"],
                "total": total_str,
                # total_volume is the more explicit name for the
                # unfilled-quantity sum. Both keys carry the same
                # value so existing consumers of ``total`` continue
                # to work.
                "total_volume": total_str,
                "vwap": (
                    # VWAP is always rendered with 2 decimal places —
                    # standard financial precision.
                    _format_decimal(vwap_dec, precision=2)
                    if vwap_dec is not None else None
                ),
                "min_price": _format_decimal(bucket["_min_price"]),
                "max_price": _format_decimal(bucket["_max_price"]),
            })

        # Sort: by symbol lexicographically (upper-cased),
        # then by side with buy before sell.
        def _sort_key(item: dict) -> tuple[str, int]:
            side_priority = 0 if item["side"] == "buy" else 1
            return (item["symbol"], side_priority)

        result.sort(key=_sort_key)
        return result

    @staticmethod
    def _format_open_orders_rich_message(
        orders: list, exchange_label: str, account: str,
        total_count: int,
    ) -> str:
        """Render the rich Open Orders summary view in the format
        mandated by the operator: one line per (symbol, side) with
        count, total remaining quantity, and price range.

        This is a pure presentation-layer renderer. It does not
        modify retrieval, normalization, or any exchange adapter.
        """
        summary = TradeDesk._compute_open_orders_rich_summary(orders)
        lines: list[str] = [
            f"📋 {exchange_label} Open Orders — {account}",
            "",
            f"Open orders: {total_count}",
        ]
        for item in summary:
            side = item["side"]
            emoji = "🔵" if side == "buy" else "🔴"
            symbol = item["symbol"]
            count = item["count"]
            total = item["total"]
            total_volume = item.get("total_volume")
            vwap = item.get("vwap")
            min_price = item["min_price"]
            max_price = item["max_price"]

            # Build the trailing "{total}, VWAP {vwap}, range L-H"
            # piece, omitting fields that are unavailable for this
            # exchange's order shape (e.g. AFX carries no price
            # and no remaining-qty field — the line shows only the
            # count, exactly as the operator's spec describes).
            parts: list[str] = []
            # Use ``total_volume`` (the more explicit name) when set;
            # fall back to ``total`` for back-compat with hand-built
            # callers.
            vol = total_volume if total_volume is not None else total
            if vol is not None:
                parts.append(f"total {vol}")
            if vwap is not None:
                parts.append(f"VWAP {vwap}")
            if min_price is not None and max_price is not None:
                if min_price == max_price:
                    parts.append(f"@ {min_price}")
                else:
                    parts.append(f"range {min_price}-{max_price}")
            # Single side label (matches the operator's spec format).
            tail = ", ".join(parts) if parts else ""
            verb = "order" if count == 1 else "orders"
            if tail:
                lines.append(
                    f"{emoji} {symbol} {side} — {count} {verb}, {tail}"
                )
            else:
                lines.append(
                    f"{emoji} {symbol} {side} — {count} {verb}"
                )
        return "\n".join(lines)

    def _format_open_orders_message(self, request: Mapping[str, Any], raw_result: Mapping[str, Any], exchange_label: str) -> str:
        account = request.get("account") or raw_result.get("account") or ""
        orders = raw_result.get("orders")
        if not isinstance(orders, list):
            raw = raw_result.get("exchange_response") or raw_result.get("raw_response") or []
            data = raw.get("data") if isinstance(raw, Mapping) else raw
            if isinstance(data, Mapping):
                orders = data.get("items") or data.get("orders") or data.get("rows") or []
            else:
                orders = data
        count = raw_result.get("open_order_count")
        if count is None:
            count = len(orders) if isinstance(orders, list) else 0

        # Pure presentation enhancement: when the canonical ``orders``
        # list is available, render the rich per-(symbol, side)
        # summary. This view is exchange-agnostic — it reads whatever
        # fields are present in the canonical order dict. Legacy
        # renderers (per-symbol buy/sell count) remain as the
        # fallback for adapters that only populate ``order_summary``.
        if isinstance(orders, list) and orders:
            return self._format_open_orders_rich_message(
                orders, exchange_label, account, count,
            )

        lines = [f"📋 {exchange_label} Open Orders — {account}", "", f"Open orders: {count}"]
        summary = raw_result.get("order_summary") or raw_result.get("order_groups")
        if isinstance(summary, list) and summary:
            lines.append("")
            for item in summary:
                if not isinstance(item, Mapping):
                    continue
                if "buy_count" in item or "sell_count" in item:
                    symbol = item.get("display_symbol") or item.get("symbol") or item.get("exchange_symbol") or "UNKNOWN"
                    buy = int(item.get("buy_count") or 0)
                    sell = int(item.get("sell_count") or 0)
                    if buy > 0:
                        lines.append(f"🔵 {symbol} {buy}")
                    if sell > 0:
                        lines.append(f"🔴 {symbol} {sell}")
                    continue
                if "count" in item:
                    side = str(item.get("side") or "").lower()
                    emoji = "🔵" if side == "buy" else "🔴" if side == "sell" else "⚪️"
                    symbol = item.get("display_symbol") or item.get("symbol") or item.get("exchange_symbol") or "UNKNOWN"
                    lines.append(f"{emoji} {symbol} {int(item.get('count') or 0)}")
                    continue
                symbol = item.get("symbol") or "UNKNOWN"
                buy = int(item.get("buy") or 0)
                sell = int(item.get("sell") or 0)
                lines.append(f"• {symbol}: {buy} buy orders, {sell} sell orders")
        return "\n".join(lines)

    def _format_ladder_chunked_message(
        self, request: Mapping[str, Any], raw_result: Mapping[str, Any],
        exchange_label: str, child_results: list, count: int, mode_line: str,
    ) -> str:
        """Render the chunked-ladder message for Hyperliquid placement.

        Same semantics as the chunked cancel renderer: every status
        word is driven by the verified count, never by exchange
        acceptance alone.
        - "Verified success" (✅) is reported only when
          verified_success is True.
        - "Partial" (⚠️) is reported only when 0 < verified_resting_count
          < total_child_orders. Stop-on-first-failure is the only way
          verified can be less than total.
        - "Could not be verified" (❌ with mismatch branch) is reported
          when the exchange accepted some children but the post-read
          found zero (or fewer) actually resting.
        - "Failed" (❌) is reported when no chunks succeeded, no
          verification agreed, or the chunk loop never produced a
          verifiable response. Includes a "Failed chunk: N" line with
          one-based numbering.
        """
        account = request.get("account") or raw_result.get("account") or ""
        verified = int(raw_result.get("verified_resting_count") or 0)
        total = int(raw_result.get("total_child_orders") or count or 0)
        chunks_succeeded = int(raw_result.get("chunks_succeeded") or 0)
        chunks_planned = int(raw_result.get("chunks_planned") or 0)
        failed_chunk_number = raw_result.get("failed_chunk_number")
        failed_chunk_error = raw_result.get("failed_chunk_error")
        verified_success = bool(raw_result.get("verified_success"))
        partial_success = bool(raw_result.get("partial_success"))
        verification_mismatch = bool(raw_result.get("verification_mismatch"))

        if verified_success:
            return (
                f"✅ {exchange_label} ladder submitted — {account}\n\n"
                f"Child orders: {total}{mode_line}\n"
                f"Verified resting: {verified}/{total}\n"
                f"Chunks: {chunks_succeeded}/{chunks_planned}"
            )
        if partial_success:
            chunks_failed = int(raw_result.get("chunks_failed") or 0)
            remaining = int(raw_result.get("remaining_target_count") or 0)
            return (
                f"⚠️ {exchange_label} ladder partially submitted — {account}\n\n"
                f"Verified resting: {verified}/{total}\n"
                f"Chunks succeeded: {chunks_succeeded}/{chunks_planned}"
                f"{f'; Failed chunk: {failed_chunk_number}' if failed_chunk_number else ''}"
                f"\nRemaining targets: {remaining}\n"
                f"No retry was attempted."
            )
        if verification_mismatch:
            exchange_accepted = int(raw_result.get("exchange_accepted_count") or 0)
            return (
                f"❌ {exchange_label} ladder could not be verified — {account}\n\n"
                f"Exchange-reported accepted: {exchange_accepted}\n"
                f"Verified resting: {verified}/{total}\n"
                f"Chunks exchange-accepted: {chunks_succeeded}/{chunks_planned}"
                f"{f'; Failed chunk: {failed_chunk_number}' if failed_chunk_number else ''}\n"
                f"No retry was attempted."
            )
        # Stop-on-failure with no verification agreement, or no exchange
        # acceptance at all.
        reason = (
            failed_chunk_error
            or raw_result.get("error")
            or "Hyperliquid ladder placement failed"
        )
        chunks_failed = int(raw_result.get("chunks_failed") or 0)
        remaining = int(raw_result.get("remaining_target_count") or 0)
        return (
            f"❌ {exchange_label} ladder not confirmed — {account}\n\n"
            f"Child orders: {total}{mode_line}\n"
            f"{reason}\n"
            f"Chunks succeeded: {chunks_succeeded}/{chunks_planned}"
            f"{f'; Failed chunk: {failed_chunk_number}' if failed_chunk_number else ''}"
            f"\nRemaining targets: {remaining}"
        )

    def _format_cancel_orders_message(
        self, request: Mapping[str, Any], raw_result: Mapping[str, Any], exchange_label: str
    ) -> str:
        """Render a verified-cancellation message for exchanges that opt in.

        Strict rules (per the chunked-cancel contract):
        - "Verified success" (✅) is reported only when the chunked cancel
          path completed all planned chunks AND the independent post-read
          confirmed every matched target OID is no longer open.
        - "Partial" (⚠️) is reported only when 0 < verified_canceled_count
          < matched_order_count. The post-read, never the exchange's
          per-child status acknowledgement alone, decides partial vs
          mismatch vs failure.
        - "Could not be verified" (❌ with mismatch branch) is reported when
          the exchange accepted one or more cancellations but the post-read
          shows zero target OIDs removed — we cannot claim partial success
          because we cannot prove any of them.
        - "Failed" (❌) is reported when no chunks succeeded, when no
          verification agreed with chunked success, or when the chunk loop
          never produced a verifiable exchange response.
        - The number shown is the verified count from the independent
          post-read, NEVER the submitted-request or per-child-status count.
        - "Failed chunk: N" displays ONE-BASED chunk numbering for humans;
          the internal ``failed_chunk_index`` field stays 0-based.

        For exchanges that do not return ``verified_success`` (e.g. legacy
        agents like AFX that report only ``success`` and ``canceled_count``),
        we fall back to the pre-chunked message so existing flows keep their
        behaviour.
        """
        account = request.get("account") or raw_result.get("account") or ""

        # If the result does not carry chunked-cancel diagnostics at all,
        # fall back to the legacy single-shot rendering for unaffected
        # exchanges (AFX, Pacifica, etc.).
        if "verified_success" not in raw_result:
            return (
                f"✅ {exchange_label} cancel orders complete — {account}\n\n"
                f"Canceled: {raw_result.get('canceled_count', 0)}"
            )

        verified = bool(raw_result.get("verified_success"))
        if verified:
            verified_canceled = int(raw_result.get("verified_canceled_count") or 0)
            chunks_succeeded = int(raw_result.get("chunks_succeeded") or 0)
            chunks_planned = int(raw_result.get("chunks_planned") or 0)
            return (
                f"✅ {exchange_label} cancel orders complete — {account}\n\n"
                f"Verified canceled: {verified_canceled}\n"
                f"Remaining targets: 0\n"
                f"Chunks succeeded: {chunks_succeeded}/{chunks_planned}"
            )

        # The four pre-conditions below are independent and the order of
        # evaluation matters: a verified-canceled count of zero ALWAYS
        # blocks the "partially completed" rendering, regardless of how
        # many chunks the exchange claimed were accepted.
        matched = int(raw_result.get("matched_order_count") or 0)
        verified_canceled = int(raw_result.get("verified_canceled_count") or 0)
        remaining = int(raw_result.get("remaining_target_count") or matched)
        exchange_accepted_count = int(raw_result.get("exchange_accepted_count") or 0)
        chunks_succeeded = int(raw_result.get("chunks_succeeded") or 0)
        chunks_planned = int(raw_result.get("chunks_planned") or 0)
        # 1-based chunk number for Telegram display; falls back to the
        # raw 0-based value if the 1-based conversion is unavailable.
        failed_chunk_display = (
            raw_result.get("failed_chunk_number")
            if raw_result.get("failed_chunk_number") is not None
            else raw_result.get("failed_chunk_index")
        )
        partial_success_flag = bool(raw_result.get("partial_success"))
        verification_mismatch_flag = bool(raw_result.get("verification_mismatch"))
        exchange_error = (
            raw_result.get("failed_chunk_error")
            or raw_result.get("error")
            or raw_result.get("verification_error")
            or "Exchange response not recognized as verified success"
        )

        # Case 1: partial — verified > 0 AND verified < matched.
        if partial_success_flag:
            return (
                f"⚠️ {exchange_label} cancellation partially completed — {account}\n\n"
                f"Verified canceled: {verified_canceled}\n"
                f"Remaining targets: {remaining}\n"
                f"Chunks succeeded: {chunks_succeeded}/{chunks_planned}\n"
                f"Failed chunk: {failed_chunk_display}\n"
                f"No retry was attempted."
            )

        # Case 2: mismatch — exchange accepted cancels but post-read saw zero.
        if verification_mismatch_flag:
            return (
                f"❌ {exchange_label} cancellation could not be verified\n\n"
                f"Exchange-reported accepted: {exchange_accepted_count}\n"
                f"Verified canceled: 0\n"
                f"Remaining targets: {matched if matched else 0}\n"
                f"Chunks exchange-accepted: {chunks_succeeded}/{chunks_planned}\n"
                f"Failed chunk: {failed_chunk_display}\n"
                f"No retry was attempted."
            )

        # Case 3: complete failure / unknown.
        if chunks_succeeded > 0:
            return (
                f"❌ {exchange_label} cancellation could not be verified\n\n"
                f"Exchange-reported accepted: {exchange_accepted_count}\n"
                f"Verified canceled: {verified_canceled}\n"
                f"Remaining targets: {remaining}\n"
                f"Chunks exchange-accepted: {chunks_succeeded}/{chunks_planned}\n"
                f"Failed chunk: {failed_chunk_display}\n"
                f"No retry was attempted."
            )

        return (
            f"❌ {exchange_label} cancellation failed — {account}\n\n"
            f"Verified canceled: 0\n"
            f"Remaining targets: {matched if matched else 0}\n"
            f"Exchange error: {exchange_error}"
        )

    def _format_balance_message(self, request: Mapping[str, Any], raw_result: Mapping[str, Any], exchange_label: str) -> str:
        account = request.get("account") or raw_result.get("account") or ""
        raw = raw_result.get("exchange_response") or raw_result.get("raw_response") or {}
        if not isinstance(raw, Mapping):
            return f"💰 {exchange_label} Balance — {account}\n\nBalance data unavailable."
        data = raw.get("data") if isinstance(raw.get("data"), Mapping) else raw_result.get("balance")
        if isinstance(data, Mapping) and exchange_label == "Pacifica":
            lines = [
                f"💰 Pacifica Balance — {account}",
                "",
                f"Account Equity: {self._money(data.get('account_equity'))}",
                f"Balance: {self._money(data.get('balance'))}",
                f"Available To Spend: {self._money(data.get('available_to_spend'))}",
                f"Available To Withdraw: {self._money(data.get('available_to_withdraw'))}",
                f"Margin Used: {self._money(data.get('total_margin_used'))}",
            ]
            return "\n".join(lines)
        balance = raw_result.get("balance") if isinstance(raw_result.get("balance"), Mapping) else {}
        # Prefer the normalized balance object if present; fall back to raw
        # exchange-specific fields otherwise. Missing/unrecognized values are
        # rendered as unavailable rather than silently as $0.00.
        def _first_value(*values: Any) -> Any:
            for value in values:
                if value is None:
                    continue
                if isinstance(value, str) and not value.strip():
                    continue
                return value
            return None
        margin = raw.get("marginSummary") or raw.get("crossMarginSummary") or raw_result.get("margin_summary") or {}
        if not isinstance(margin, Mapping):
            margin = {}
        account_value = _first_value(
            balance.get("account_value") if isinstance(balance, Mapping) else None,
            balance.get("account_equity") if isinstance(balance, Mapping) else None,
            margin.get("accountValue"),
            raw.get("account_value"),
            raw.get("accountValue"),
        )
        withdrawable = _first_value(
            balance.get("withdrawable") if isinstance(balance, Mapping) else None,
            balance.get("available_to_withdraw") if isinstance(balance, Mapping) else None,
            raw.get("withdrawable"),
            raw.get("withdrawableUsd"),
            raw.get("availableToWithdraw"),
        )
        margin_used = _first_value(
            balance.get("margin_used") if isinstance(balance, Mapping) else None,
            balance.get("total_margin_used") if isinstance(balance, Mapping) else None,
            margin.get("totalMarginUsed"),
            raw.get("margin_used"),
        )
        total_position_value = _first_value(
            balance.get("total_position_value") if isinstance(balance, Mapping) else None,
            margin.get("totalNtlPos"),
            raw.get("total_position_value"),
            raw.get("totalPositionValue"),
        )
        if account_value is None and withdrawable is None and margin_used is None and total_position_value is None:
            return f"💰 {exchange_label} Balance — {account}\n\nBalance unavailable."
        lines = [
            f"💰 {exchange_label} Balance — {account}",
            "",
            f"Account Value: {'Balance unavailable' if account_value is None else self._money(account_value)}",
            f"Withdrawable: {'Balance unavailable' if withdrawable is None else self._money(withdrawable)}",
            f"Margin Used: {'Balance unavailable' if margin_used is None else self._money(margin_used)}",
            f"Total Position Value: {'Balance unavailable' if total_position_value is None else self._money(total_position_value)}",
        ]
        positions = raw.get("assetPositions") or raw.get("positions") or []
        rendered_positions = []
        if isinstance(positions, list):
            is_rise = (exchange_label.lower() == "rise")
            for item in positions:
                pos = item.get("position", item) if isinstance(item, Mapping) else {}
                if not isinstance(pos, Mapping):
                    continue
                if is_rise:
                    # Rise uses normalized keys: ``symbol``, ``side``,
                    # ``size``, ``entry_price``, ``unrealized_pnl``.
                    coin = (
                        pos.get("symbol")
                        or pos.get("coin")
                        or ""
                    )
                    side = (
                        pos.get("side") or ""
                    ).lower() or "long"
                    szi = pos.get("size")
                    entry = pos.get("entry_price")
                    pnl = pos.get("unrealized_pnl")
                else:
                    # Hyperliquid (and similar) wire field names.
                    szi = pos.get("szi") or pos.get("size") or pos.get("position")
                    entry = pos.get("entryPx")
                    pnl = pos.get("unrealizedPnl") or pos.get("pnl") or 0
                    coin = pos.get("coin", "")
                    side = "long"
                try:
                    size_float = float(szi) if szi is not None else 0.0
                except Exception:
                    size_float = 0.0
                if size_float == 0:
                    continue
                if not is_rise:
                    side = "long" if size_float > 0 else "short"
                try:
                    pnl_prefix = "+" if float(pnl) >= 0 else ""
                except Exception:
                    pnl_prefix = ""
                rendered_positions.append(
                    f"• {coin} {side} {self._number(abs(size_float))}",
                )
                rendered_positions.append(
                    f"  Entry: {self._price(entry)}",
                )
                rendered_positions.append(
                    f"  PnL: {pnl_prefix}{self._money(pnl)}",
                )
                if is_rise:
                    # Rise has no Liq field (cross-margin); skip.
                    continue
                rendered_positions.append(
                    f"  Liq: {self._price(pos.get('liquidationPx') or pos.get('liqPx'))}",
                )
        lines.extend(["", "Open Positions:"])
        lines.extend(rendered_positions or ["• None"])
        return "\n".join(lines)

    @staticmethod
    def _exchange(request: Mapping[str, Any]) -> str:
        return str(request.get("exchange") or "").lower()

    @staticmethod
    def _error(request: Mapping[str, Any], error: str, error_type: Optional[str] = None) -> dict:
        result = {
            "success": False,
            "exchange": str(request.get("exchange") or "").lower() or None,
            "operation": request.get("operation"),
            "error": error,
            "structured_request": dict(request),
        }
        if error_type:
            result["error_type"] = error_type
        return result
