"""ApeX Omni ExchangeAgent.

Phase 1: account discovery.
Phase 2: balance only.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_DOWN
import traceback
from typing import Any, Callable, Mapping, Optional

from .account_discovery import combined_casefold_env

APEX_REQUIRED_SUFFIXES = (
    "ACCOUNTID",
    "APIKEY",
    "APIKEYSECRET",
    "APIKEYPASSPHRASE",
    "SEEDS",
    "L2KEY",
)


class ApexAgent:
    """ApeX-specific agent boundary.

    All ApeX-specific SDK/API behavior belongs here. Phase 2 intentionally
    implements only account discovery and balance.
    """

    exchange = "apex"

    def __init__(self, *, client_factory: Optional[Callable[..., Any]] = None) -> None:
        self.client_factory = client_factory
        self.last_response_shape: dict[str, str] = {}
        self._last_config_v3: Mapping[str, Any] = {}

    def list_accounts(self) -> dict:
        """Return Apex account aliases with a complete credential set.

        Discovery is driven by the account marker variable
        ``APEX_<ACCOUNT>_ACCOUNTID``. An alias is returned only if every
        required ``APEX_<ACCOUNT>_*`` credential variable exists with a non-empty
        value in process env or the active Hermes ``.env``. Secret values are
        never returned.
        """
        env = combined_casefold_env()
        aliases: set[str] = set()
        prefix = "APEX_"
        marker_suffix = "_ACCOUNTID"

        for actual_key, _value, _source in env.values():
            upper_key = actual_key.upper()
            if not upper_key.startswith(prefix) or not upper_key.endswith(marker_suffix):
                continue
            raw_account = actual_key[len(prefix) : -len(marker_suffix)]
            alias = self._normalize_alias(raw_account)
            if not alias:
                continue
            if self._has_complete_credentials(raw_account, env):
                aliases.add(alias)

        accounts = sorted(aliases)
        return {
            "success": True,
            "exchange": self.exchange,
            "accounts": accounts,
            "message": f"Found {len(accounts)} ApeX configured account(s).",
        }

    def execute(self, request: Mapping[str, Any]) -> dict:
        """Execute the currently supported ApeX operation."""
        operation = str(request.get("operation") or "").lower() if isinstance(request, Mapping) else ""
        if operation == "balance":
            return self._balance(request)
        if operation == "order":
            return self._order(request)
        if operation == "open_orders":
            return self._open_orders(request)
        if operation == "cancel_orders":
            return self._cancel_orders(request)
        if operation == "batch_orders":
            return self._batch_orders(request)
        if operation == "positions":
            return self._positions(request)
        if operation in {"set_tp", "set_sl"}:
            return self._set_tpsl(request, operation)
        return {
            "success": False,
            "exchange": self.exchange,
            "operation": request.get("operation") if isinstance(request, Mapping) else None,
            "error": "ApeX Phase 4 supports account discovery, balance, single orders, and open orders only.",
            "structured_request": dict(request) if isinstance(request, Mapping) else request,
        }

    def _balance(self, request: Mapping[str, Any]) -> dict:
        alias = str(request.get("account") or "").strip()
        credentials, error = self._resolve_credentials(alias)
        if error:
            return {
                "success": False,
                "exchange": self.exchange,
                "operation": "balance",
                "account": alias,
                "error": error,
            }
        try:
            client = self._client_for_credentials(credentials)
            raw_balance = client.get_account_balance_v3()
            raw_account = client.get_account_v3()
            normalized = self._normalize_balance(raw_balance, raw_account)
            return {
                "success": True,
                "exchange": self.exchange,
                "operation": "balance",
                "parent_operation": "balance",
                "account": alias,
                "exchange_response": normalized,
                "raw_balance": raw_balance,
                "raw_account": raw_account,
                "response_shape": dict(self.last_response_shape),
            }
        except Exception as exc:
            return {
                "success": False,
                "exchange": self.exchange,
                "operation": "balance",
                "parent_operation": "balance",
                "account": alias,
                "error": str(exc),
                "error_type": exc.__class__.__name__,
            }

    def _positions(self, request: Mapping[str, Any]) -> dict:
        alias = str(request.get("account") or "").strip()
        credentials, error = self._resolve_credentials(alias)
        if error:
            return {"success": False, "exchange": self.exchange, "operation": "positions", "parent_operation": "positions", "account": alias, "error": error}
        try:
            client = self._client_for_credentials(credentials)
            raw_account = client.get_account_v3()
            raw_positions = self._as_mapping(raw_account).get("positions")
            positions = []
            for raw in raw_positions if isinstance(raw_positions, list) else []:
                if not isinstance(raw, Mapping):
                    continue
                pos = self._normalize_apex_position(raw)
                try:
                    size_float = abs(float(pos.get("size") or 0))
                except Exception:
                    size_float = 0.0
                if size_float > 0:
                    positions.append(pos)
            tpsl_enrichment = self._enrich_positions_with_tpsl(client, positions)
            result = {
                "success": True,
                "exchange": self.exchange,
                "operation": "positions",
                "parent_operation": "positions",
                "account": alias,
                "positions": positions,
                "position_count": len(positions),
                "exchange_response": raw_account,
                "raw_response": raw_account,
            }
            if tpsl_enrichment:
                result["tpsl_enrichment"] = tpsl_enrichment
            return result
        except Exception as exc:
            return {"success": False, "exchange": self.exchange, "operation": "positions", "parent_operation": "positions", "account": alias, "error": str(exc), "error_type": exc.__class__.__name__}

    def _set_tpsl(self, request: Mapping[str, Any], operation: str) -> dict:
        structured = self._as_mapping(request.get("structured_request"))
        effective_request = dict(structured)
        effective_request.update(dict(request))
        alias = str(effective_request.get("account") or "").strip()
        credentials, error = self._resolve_credentials(alias)
        if error:
            return {"success": False, "exchange": self.exchange, "operation": operation, "parent_operation": operation, "account": alias, "error": error}
        try:
            client = self._client_for_credentials(credentials)
            client.get_account_v3()
            position = self._as_mapping(effective_request.get("position"))
            symbol = self._trade_symbol_for(effective_request.get("symbol") or position.get("symbol") or position.get("exchange_symbol"))
            pos_side = str(position.get("side") or effective_request.get("side") or "").lower()
            close_side = "SELL" if pos_side in {"long", "buy", "bid"} else "BUY"
            pos_size = abs(Decimal(str(position.get("size") or effective_request.get("size") or 0)))
            rules = self._instrument_rules(symbol, self._last_config_v3)
            size = self._normalize_size(pos_size, rules)
            trigger_raw = effective_request.get("price")
            trigger = Decimal(str(trigger_raw))
            leg = "tp" if operation == "set_tp" else "sl"
            existing = self._find_existing_tpsl(client, symbol, leg)
            cancel_responses = []
            for order in existing:
                order_id = self._first_value([order], "id", "orderId", "order_id")
                if order_id:
                    cancel_responses.append(client.delete_order_v3(id=order_id))
            if trigger == 0:
                return {
                    "success": True,
                    "exchange": self.exchange,
                    "operation": operation,
                    "parent_operation": operation,
                    "account": alias,
                    "symbol": symbol,
                    "removed": len(cancel_responses),
                    "cancel_responses": cancel_responses,
                    "message": f"✅ Apex {leg.upper()} removed — {alias}\n\n{symbol}",
                }
            price = self._normalize_price(trigger, rules)
            order_type = "TAKE_PROFIT_MARKET" if operation == "set_tp" else "STOP_MARKET"
            sdk_payload = {
                "symbol": symbol,
                "side": close_side,
                "type": order_type,
                "size": size,
                "price": price,
                "reduceOnly": True,
                "isPositionTpsl": True,
                "triggerPrice": price,
                "triggerPriceType": "MARKET",
            }
            raw = client.create_order_v3(**sdk_payload)
            order_id = self._extract_order_id(raw)
            return {
                "success": bool(order_id),
                "exchange": self.exchange,
                "operation": operation,
                "parent_operation": operation,
                "account": alias,
                "symbol": symbol,
                "order_id": order_id,
                "removed": len(cancel_responses),
                "cancel_responses": cancel_responses,
                "sdk_payload": sdk_payload,
                "exchange_response": raw,
                "raw_response": raw,
                "message": f"✅ Apex {leg.upper()} set — {alias}\n\n{symbol} @ {price}" if order_id else f"❌ Apex {leg.upper()} not confirmed — {alias}",
            }
        except Exception as exc:
            return {
                "success": False,
                "exchange": self.exchange,
                "operation": operation,
                "parent_operation": operation,
                "account": alias,
                "error": str(exc) or repr(exc),
                "error_type": exc.__class__.__name__,
                "traceback": traceback.format_exc(),
            }

    def _cancel_orders(self, request: Mapping[str, Any]) -> dict:
        alias = str(request.get("account") or "").strip()
        credentials, error = self._resolve_credentials(alias)
        if error:
            return {
                "success": False,
                "exchange": self.exchange,
                "operation": "cancel_orders",
                "parent_operation": "cancel_orders",
                "account": alias,
                "error": error,
            }
        symbol = self._trade_symbol_for(request.get("symbol"))
        try:
            client = self._client_for_credentials(credentials)
            sdk_payload = {"symbol": symbol}
            raw = client.delete_open_orders_v3(**sdk_payload)
            return {
                "success": True,
                "exchange": self.exchange,
                "operation": "cancel_orders",
                "parent_operation": "cancel_orders",
                "account": alias,
                "symbol": symbol,
                "canceled_symbol": symbol,
                "sdk_payload": sdk_payload,
                "exchange_response": raw,
                "raw_response": raw,
            }
        except Exception as exc:
            return {
                "success": False,
                "exchange": self.exchange,
                "operation": "cancel_orders",
                "parent_operation": "cancel_orders",
                "account": alias,
                "symbol": symbol,
                "error": str(exc),
                "error_type": exc.__class__.__name__,
            }

    def _open_orders(self, request: Mapping[str, Any]) -> dict:
        alias = str(request.get("account") or "").strip()
        credentials, error = self._resolve_credentials(alias)
        if error:
            return {
                "success": False,
                "exchange": self.exchange,
                "operation": "open_orders",
                "parent_operation": "open_orders",
                "account": alias,
                "error": error,
            }
        try:
            client = self._client_for_credentials(credentials)
            raw = client.open_orders_v3()
            raw_orders, shape = self._extract_open_orders(raw)
            orders = [self._normalize_open_order(order) for order in raw_orders if isinstance(order, Mapping)]
            side_groups = self._group_open_orders(orders)
            symbol_groups = self._group_open_orders_by_symbol(orders)
            return {
                "success": True,
                "exchange": self.exchange,
                "operation": "open_orders",
                "parent_operation": "open_orders",
                "account": alias,
                "orders": orders,
                "open_order_count": len(orders),
                "order_groups": symbol_groups,
                "symbol_groups": symbol_groups,
                "side_order_groups": side_groups,
                "order_summary": symbol_groups,
                "exchange_response": raw,
                "raw_response": raw,
                "response_shape": shape,
            }
        except Exception as exc:
            return {
                "success": False,
                "exchange": self.exchange,
                "operation": "open_orders",
                "parent_operation": "open_orders",
                "account": alias,
                "error": str(exc),
                "error_type": exc.__class__.__name__,
            }

    def _batch_orders(self, request: Mapping[str, Any]) -> dict:
        alias = str(request.get("account") or "").strip()
        distribution = str(request.get("distribution") or "").lower()
        if distribution not in {"half_gaussian", "uniform", "equal", "equal_distribution"}:
            return {
                "success": False,
                "exchange": self.exchange,
                "operation": "batch_orders",
                "parent_operation": request.get("parent_operation") or "ladder",
                "account": alias,
                "error": "ApeX ladder supports batch_orders only for distribution=half_gaussian or uniform/equal.",
            }
        credentials, error = self._resolve_credentials(alias)
        if error:
            return {
                "success": False,
                "exchange": self.exchange,
                "operation": "batch_orders",
                "parent_operation": request.get("parent_operation") or "ladder",
                "account": alias,
                "error": error,
            }
        child_orders = request.get("child_orders")
        if not isinstance(child_orders, list) or not child_orders:
            return {
                "success": False,
                "exchange": self.exchange,
                "operation": "batch_orders",
                "parent_operation": request.get("parent_operation") or "ladder",
                "account": alias,
                "error": "ApeX half_gaussian ladder requires child_orders from TradeDesk.",
            }
        try:
            client = self._client_for_credentials(credentials)
            client.get_account_v3()
            child_results = []
            raw_results = []
            sdk_payloads = []
            all_success = True
            for raw_child in child_orders:
                child = self._as_mapping(raw_child)
                try:
                    sdk_payload = self._sdk_payload_for_child(client, child)
                    raw = client.create_order_v3(**sdk_payload)
                    order_id = self._extract_order_id(raw)
                    success = bool(order_id)
                    all_success = all_success and success
                    sdk_payloads.append(sdk_payload)
                    raw_results.append(raw)
                    child_result = self._child_result(child, sdk_payload, raw, success=success, order_id=order_id)
                    if not success:
                        child_result["error"] = "missing order id"
                    child_results.append(child_result)
                except Exception as exc:
                    all_success = False
                    child_results.append({
                        "child_id": child.get("child_id"),
                        "success": False,
                        "symbol": self._trade_symbol_for(child.get("symbol")) if child.get("symbol") else None,
                        "side": str(child.get("side") or "").lower(),
                        "order_type": str(child.get("order_type") or "").lower(),
                        "child_order": dict(child),
                        "error": str(exc),
                        "error_type": exc.__class__.__name__,
                    })
            return {
                "success": all_success,
                "exchange": self.exchange,
                "operation": "batch_orders",
                "parent_operation": request.get("parent_operation") or "ladder",
                "distribution": request.get("distribution"),
                "account": alias,
                "submitted_count": len([child for child in child_results if child.get("success")]),
                "child_results": child_results,
                "sdk_payload": {"method": "create_order_v3", "mode": "one_by_one", "order_requests": sdk_payloads},
                "exchange_response": raw_results,
                "raw_response": raw_results,
            }
        except Exception as exc:
            return {
                "success": False,
                "exchange": self.exchange,
                "operation": "batch_orders",
                "parent_operation": request.get("parent_operation") or "ladder",
                "account": alias,
                "error": str(exc),
                "error_type": exc.__class__.__name__,
            }

    def _order(self, request: Mapping[str, Any]) -> dict:
        alias = str(request.get("account") or "").strip()
        credentials, error = self._resolve_credentials(alias)
        if error:
            return {
                "success": False,
                "exchange": self.exchange,
                "operation": "order",
                "parent_operation": request.get("parent_operation") or "place_order",
                "account": alias,
                "error": error,
            }
        child = self._as_mapping(request.get("child_order"))
        if not child:
            child_orders = request.get("child_orders")
            if isinstance(child_orders, list) and child_orders:
                child = self._as_mapping(child_orders[0])
        if not child:
            return self._order_error(request, alias, "ApeX order requires child_order")
        try:
            client = self._client_for_credentials(credentials)
            account_snapshot = client.get_account_v3()
            sdk_payload = self._sdk_payload_for_child(client, child)
            raw = client.create_order_v3(**sdk_payload)
            order_id = self._extract_order_id(raw)
            verification = self._verify_order_visible(client, order_id) if order_id else {"visible": False, "reason": "missing order id"}
            child_success = bool(order_id) and bool(verification.get("visible", True))
            child_result = self._child_result(child, sdk_payload, raw, success=child_success, order_id=order_id, verification=verification)
            if not child_success:
                child_result["error"] = verification.get("reason") or "ApeX order was not verified as visible"
            return {
                "success": child_success,
                "exchange": self.exchange,
                "operation": "order",
                "parent_operation": request.get("parent_operation") or "place_order",
                "account": alias,
                "child_results": [child_result],
                "child_order": dict(child),
                "sdk_payload": sdk_payload,
                "exchange_response": raw,
                "raw_response": raw,
                "account_snapshot_shape": "data" if isinstance(account_snapshot, Mapping) and isinstance(account_snapshot.get("data"), Mapping) else "top-level",
            }
        except Exception as exc:
            return self._order_error(request, alias, str(exc), exc.__class__.__name__)

    def _sdk_payload_for_child(self, client: Any, child: Mapping[str, Any]) -> dict[str, str]:
        symbol = self._trade_symbol_for(child.get("symbol"))
        rules = self._instrument_rules(symbol, self._last_config_v3)
        side = "BUY" if str(child.get("side") or "").lower() == "buy" else "SELL"
        order_type = str(child.get("order_type") or "").upper()
        if order_type not in {"LIMIT", "MARKET"}:
            raise ValueError(f"Unsupported ApeX order type: {order_type}")
        size = self._normalize_size(child.get("size"), rules)
        raw_price = child.get("price")
        if order_type == "MARKET" and raw_price in (None, ""):
            raw_price = self._market_price_for(client, symbol, side)
        if raw_price in (None, ""):
            raise ValueError("ApeX limit order requires price")
        price = self._normalize_price(raw_price, rules)
        return {"symbol": symbol, "side": side, "type": order_type, "size": size, "price": price}

    def _child_result(self, child: Mapping[str, Any], sdk_payload: Mapping[str, Any], raw: Any, *, success: bool, order_id: Any, verification: Optional[Mapping[str, Any]] = None) -> dict[str, Any]:
        result = {
            "child_id": child.get("child_id"),
            "success": success,
            "symbol": sdk_payload.get("symbol"),
            "side": str(child.get("side") or "").lower(),
            "order_type": str(child.get("order_type") or "").lower(),
            "size": sdk_payload.get("size"),
            "price": sdk_payload.get("price"),
            "order_id": order_id,
            "child_order": dict(child),
            "sdk_payload": dict(sdk_payload),
            "exchange_response": raw,
        }
        if verification is not None:
            result["verification"] = dict(verification)
        return result

    def _order_error(self, request: Mapping[str, Any], alias: str, error: str, error_type: Optional[str] = None) -> dict:
        result = {
            "success": False,
            "exchange": self.exchange,
            "operation": "order",
            "parent_operation": request.get("parent_operation") or "place_order",
            "account": alias,
            "error": error,
        }
        if error_type:
            result["error_type"] = error_type
        return result

    def _client_for_credentials(self, credentials: Mapping[str, str]) -> Any:
        """Create and initialize the official non-RWA ApeX v3 private client."""
        if self.client_factory is not None:
            client = self.client_factory(credentials)
        else:
            from apexomni.constants import APEX_OMNI_HTTP_MAIN
            from apexomni.http_private_sign import HttpPrivateSign

            client = HttpPrivateSign(
                APEX_OMNI_HTTP_MAIN,
                api_key_credentials={
                    "key": credentials["apikey"],
                    "secret": credentials["apikeysecret"],
                    "passphrase": credentials["apikeypassphrase"],
                },
                zk_seeds=credentials["seeds"],
                zk_l2Key=credentials["l2key"],
            )
            # In apexomni 3.3.1 get_account_balance_v3 is implemented only on
            # the RWA subclass even though it calls the regular /v3/account-balance
            # endpoint. RWA is out of scope, so keep the non-RWA client and attach
            # the same regular endpoint method locally.
            if not hasattr(client, "get_account_balance_v3"):
                def get_account_balance_v3(**kwargs: Any) -> Any:
                    from apexomni.constants import URL_SUFFIX
                    return client._get(endpoint=URL_SUFFIX + "/v3/account-balance", params=kwargs)
                client.get_account_balance_v3 = get_account_balance_v3  # type: ignore[attr-defined]
            if not hasattr(client, "open_orders_v3"):
                def open_orders_v3(**kwargs: Any) -> Any:
                    from apexomni.constants import URL_SUFFIX
                    return client._get(endpoint=URL_SUFFIX + "/v3/open-orders", params=kwargs)
                client.open_orders_v3 = open_orders_v3  # type: ignore[attr-defined]
            if not hasattr(client, "delete_open_orders_v3"):
                def delete_open_orders_v3(**kwargs: Any) -> Any:
                    from apexomni.constants import URL_SUFFIX
                    return client._post(endpoint=URL_SUFFIX + "/v3/delete-open-orders", data=kwargs)
                client.delete_open_orders_v3 = delete_open_orders_v3  # type: ignore[attr-defined]
            if not hasattr(client, "history_orders_v3"):
                def history_orders_v3(**kwargs: Any) -> Any:
                    from apexomni.constants import URL_SUFFIX
                    return client._get(endpoint=URL_SUFFIX + "/v3/history-orders", params=kwargs)
                client.history_orders_v3 = history_orders_v3  # type: ignore[attr-defined]
            if not hasattr(client, "delete_order_v3"):
                def delete_order_v3(**kwargs: Any) -> Any:
                    from apexomni.constants import URL_SUFFIX
                    return client._post(endpoint=URL_SUFFIX + "/v3/delete-order", data=kwargs)
                client.delete_order_v3 = delete_order_v3  # type: ignore[attr-defined]

        if hasattr(client, "configs_v3"):
            config_response = client.configs_v3()
            self._last_config_v3 = self._data_mapping(config_response) or self._as_mapping(config_response)
        return client

    @classmethod
    def _normalize_apex_position(cls, raw: Mapping[str, Any]) -> dict[str, Any]:
        exchange_symbol = str(cls._first_value([raw], "symbol", "exchange_symbol") or "").upper()
        display_symbol = cls._display_symbol_for(exchange_symbol)
        side_raw = str(cls._first_value([raw], "side", "positionSide") or "").lower()
        side = "long" if side_raw in {"long", "buy", "bid"} else "short" if side_raw in {"short", "sell", "ask"} else side_raw
        size = cls._string_or_none(cls._first_value([raw], "size", "szi", "position", "qty", "amount"))
        return {
            "id": f"{exchange_symbol}:{side}",
            "exchange_symbol": exchange_symbol,
            "display_symbol": display_symbol,
            "symbol": display_symbol,
            "side": side,
            "size": size,
            "entry_price": cls._string_or_none(cls._first_value([raw], "entryPrice", "entryPx", "avgEntryPrice")),
            "mark_price": cls._string_or_none(cls._first_value([raw], "markPrice", "oraclePrice")),
            "unrealized_pnl": cls._string_or_none(cls._first_value([raw], "unrealizedPnl", "pnl")),
            "liquidation_price": cls._string_or_none(cls._first_value([raw], "liquidationPrice", "liquidationPx", "liqPx")),
            "raw": dict(raw),
        }

    def _enrich_positions_with_tpsl(self, client: Any, positions: list[dict[str, Any]]) -> dict[str, Any]:
        """Attach Apex position TP/SL trigger prices to normalized positions.

        Apex /v3/open-orders returns regular OPEN resting orders, but live
        position TP/SL triggers are conditional orders returned by
        /v3/history-orders. Query history once per unique exchange symbol,
        filter to active UNTRIGGERED position TP/SL reduce-only triggers, then
        select at most one TP and one SL per (symbol, position side).

        Duplicate selection is deterministic and never depends on response
        order: newest active order wins by (updatedTime, createdAt,
        numeric orderId/id), with the order id string as a final stable
        fallback when numeric fields are missing or malformed.
        """
        for position in positions:
            position.setdefault("take_profit", None)
            position.setdefault("stop_loss", None)
        symbols = sorted({str(position.get("exchange_symbol") or "").upper() for position in positions if position.get("exchange_symbol")})
        if not symbols:
            return {"success": True, "source": "history_orders_v3", "symbols": []}
        by_key: dict[tuple[str, str], dict[str, Mapping[str, Any]]] = {}
        errors: list[dict[str, str]] = []
        for symbol in symbols:
            try:
                raw_orders = self._fetch_history_tpsl_orders(client, symbol)
            except Exception as exc:
                errors.append({"symbol": symbol, "error": str(exc), "error_type": exc.__class__.__name__})
                continue
            for order in raw_orders:
                if not isinstance(order, Mapping):
                    continue
                classified = self._classify_active_position_tpsl(order)
                if classified is None:
                    continue
                order_symbol, position_side, leg, trigger_price = classified
                if order_symbol != symbol:
                    continue
                key = (order_symbol, position_side)
                slot = by_key.setdefault(key, {})
                current = slot.get(leg)
                if current is None or self._tpsl_rank(order) > self._tpsl_rank(current):
                    slot[leg] = order
                    slot[f"{leg}_trigger_price"] = trigger_price  # type: ignore[index]
        for position in positions:
            key = (str(position.get("exchange_symbol") or "").upper(), str(position.get("side") or "").lower())
            slot = by_key.get(key, {})
            position["take_profit"] = slot.get("take_profit_trigger_price")
            position["stop_loss"] = slot.get("stop_loss_trigger_price")
        result: dict[str, Any] = {
            "success": not errors,
            "source": "history_orders_v3",
            "symbols": symbols,
            "pagination": {
                "status": "UNTRIGGERED",
                "orderType": "CONDITION",
                "limit": "100",
                "page_start": "0",
            },
        }
        if errors:
            result["errors"] = errors
        return result

    def _fetch_history_tpsl_orders(self, client: Any, symbol: str) -> list[Mapping[str, Any]]:
        """Read active Apex conditional orders for one symbol with pagination.

        Apex documents /v3/history-orders query params: symbol, status, type,
        limit (default 100), page (0-based), and orderType. Use the largest
        documented default page size observed in docs (100), ask only for
        UNTRIGGERED CONDITION orders, and advance page while totalSize says
        more data exists. A bounded page cap prevents pathological loops if an
        exchange response is malformed.
        """
        fetcher = getattr(client, "history_orders_v3", None)
        if not callable(fetcher):
            return []
        orders: list[Mapping[str, Any]] = []
        page = 0
        limit = 100
        max_pages = 20
        while page < max_pages:
            raw = fetcher(symbol=symbol, status="UNTRIGGERED", orderType="CONDITION", limit=str(limit), page=str(page))
            page_orders, _shape = self._extract_open_orders(raw)
            for order in page_orders:
                if isinstance(order, Mapping):
                    orders.append(order)
            total_size = self._history_total_size(raw)
            if total_size is None or len(page_orders) < limit or len(orders) >= total_size:
                break
            page += 1
        return orders

    @classmethod
    def _history_total_size(cls, raw: Any) -> Optional[int]:
        data = cls._data_mapping(raw)
        value = data.get("totalSize") if isinstance(data, Mapping) else None
        if value in (None, ""):
            return None
        try:
            return int(str(value))
        except Exception:
            return None

    @classmethod
    def _classify_active_position_tpsl(cls, order: Mapping[str, Any]) -> Optional[tuple[str, str, str, str]]:
        status = str(order.get("status") or "").upper()
        if status != "UNTRIGGERED":
            return None
        if not cls._truthy(order.get("isPositionTpsl")):
            return None
        if not cls._truthy(order.get("reduceOnly")):
            return None
        trigger = cls._valid_trigger_price(order.get("triggerPrice"))
        if trigger is None:
            return None
        order_type = str(order.get("type") or order.get("order_type") or "").upper()
        if order_type in {"TAKE_PROFIT_MARKET", "TAKE_PROFIT_LIMIT"}:
            leg = "take_profit"
        elif order_type in {"STOP_MARKET", "STOP_LIMIT"}:
            leg = "stop_loss"
        else:
            return None
        symbol = str(order.get("symbol") or order.get("exchange_symbol") or "").upper()
        side = str(order.get("side") or "").upper()
        if side == "SELL":
            position_side = "long"
        elif side == "BUY":
            position_side = "short"
        else:
            return None
        if not symbol:
            return None
        return symbol, position_side, leg, trigger

    @classmethod
    def _valid_trigger_price(cls, value: Any) -> Optional[str]:
        if value in (None, ""):
            return None
        text = str(value)
        try:
            number = Decimal(text)
        except (InvalidOperation, ValueError):
            return None
        if not number.is_finite() or number == 0:
            return None
        return text

    @staticmethod
    def _truthy(value: Any) -> bool:
        if value is True:
            return True
        if value is False or value is None:
            return False
        return str(value).strip().lower() in {"true", "1", "yes", "y"}

    @classmethod
    def _tpsl_rank(cls, order: Mapping[str, Any]) -> tuple[int, int, int, str]:
        def integer_field(*keys: str) -> int:
            for key in keys:
                value = order.get(key)
                if value in (None, ""):
                    continue
                try:
                    return int(str(value))
                except Exception:
                    continue
            return -1
        updated = integer_field("updatedTime", "updatedAt")
        created = integer_field("createdAt")
        order_id = integer_field("orderId", "id", "order_id")
        stable = str(cls._first_value([order], "orderId", "id", "order_id") or "")
        return updated, created, order_id, stable

    def _find_existing_tpsl(self, client: Any, symbol: str, leg: str) -> list[Mapping[str, Any]]:
        candidates: list[Mapping[str, Any]] = []
        for fetcher_name in ("open_orders_v3", "history_orders_v3"):
            fetcher = getattr(client, fetcher_name, None)
            if not callable(fetcher):
                continue
            raw = fetcher(symbol=symbol)
            raw_orders, _shape = self._extract_open_orders(raw)
            for order in raw_orders:
                if not isinstance(order, Mapping):
                    continue
                if self._is_active_tpsl_order(order, leg):
                    candidates.append(order)
        seen = set()
        unique: list[Mapping[str, Any]] = []
        for order in candidates:
            order_id = self._first_value([order], "id", "orderId", "order_id")
            key = str(order_id or id(order))
            if key in seen:
                continue
            seen.add(key)
            unique.append(order)
        return unique

    @classmethod
    def _is_active_tpsl_order(cls, order: Mapping[str, Any], leg: str) -> bool:
        if str(order.get("isPositionTpsl") or "").lower() not in {"true", "1"} and order.get("isPositionTpsl") is not True:
            return False
        status = str(order.get("status") or "").upper()
        if status in {"CANCELED", "CANCELLED", "FILLED", "REJECTED", "EXPIRED"}:
            return False
        typ = str(order.get("type") or order.get("order_type") or "").upper()
        if leg == "tp":
            return order.get("isSetOpenTp") is True or str(order.get("isSetOpenTp") or "").lower() in {"true", "1"} or "TAKE_PROFIT" in typ
        return order.get("isSetOpenSl") is True or str(order.get("isSetOpenSl") or "").lower() in {"true", "1"} or "STOP" in typ

    @classmethod
    def _extract_open_orders(cls, raw: Any) -> tuple[list[Any], str]:
        if isinstance(raw, list):
            return raw, "bare list"
        top = cls._as_mapping(raw)
        data = top.get("data")
        if isinstance(data, list):
            return data, "data[]"
        if isinstance(data, Mapping):
            for key, shape in (("orders", "data.orders"), ("openOrders", "data.openOrders")):
                value = data.get(key)
                if isinstance(value, list):
                    return value, shape
        return [], "unknown"

    @classmethod
    def _normalize_open_order(cls, order: Mapping[str, Any]) -> dict[str, Any]:
        exchange_symbol = str(cls._first_value([order], "symbol", "exchange_symbol", "contract", "instrumentId") or "").upper()
        display_symbol = cls._display_symbol_for(exchange_symbol)
        side_raw = str(cls._first_value([order], "side", "orderSide") or "").lower()
        side = "buy" if side_raw in {"buy", "bid", "b", "long"} else "sell" if side_raw in {"sell", "ask", "s", "short"} else side_raw
        return {
            "exchange_symbol": exchange_symbol,
            "display_symbol": display_symbol,
            "symbol": exchange_symbol,
            "side": side,
            "order_id": cls._string_or_none(cls._first_value([order], "orderId", "id", "order_id")),
            "client_order_id": cls._string_or_none(cls._first_value([order], "clientOrderId", "clientId", "client_order_id")),
            "order_type": cls._string_or_none(cls._first_value([order], "order_type", "orderType", "type")),
            "status": cls._string_or_none(cls._first_value([order], "status")),
            "price": cls._string_or_none(cls._first_value([order], "price", "limitPx", "px")),
            "size": cls._string_or_none(cls._first_value([order], "size", "sz", "qty", "amount")),
            "raw": dict(order),
        }

    @classmethod
    def _group_open_orders(cls, orders: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
        counts: dict[tuple[str, str], dict[str, Any]] = {}
        for order in orders:
            exchange_symbol = str(order.get("exchange_symbol") or order.get("symbol") or "")
            side = str(order.get("side") or "").lower()
            if not exchange_symbol or not side:
                continue
            key = (exchange_symbol, side)
            if key not in counts:
                counts[key] = {
                    "exchange_symbol": exchange_symbol,
                    "display_symbol": str(order.get("display_symbol") or cls._display_symbol_for(exchange_symbol)),
                    "side": side,
                    "count": 0,
                }
            counts[key]["count"] += 1
        groups = [group for group in counts.values() if int(group.get("count") or 0) > 0]
        return sorted(groups, key=lambda g: (str(g.get("display_symbol") or ""), str(g.get("side") or "")))

    @classmethod
    def _group_open_orders_by_symbol(cls, orders: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
        buckets: dict[str, dict[str, Any]] = {}
        for order in orders:
            exchange_symbol = str(order.get("exchange_symbol") or order.get("symbol") or "")
            if not exchange_symbol:
                continue
            bucket = buckets.setdefault(
                exchange_symbol,
                {
                    "exchange_symbol": exchange_symbol,
                    "display_symbol": str(order.get("display_symbol") or cls._display_symbol_for(exchange_symbol)),
                    "buy_count": 0,
                    "sell_count": 0,
                    "count": 0,
                },
            )
            side = str(order.get("side") or "").lower()
            if side == "buy":
                bucket["buy_count"] += 1
            elif side == "sell":
                bucket["sell_count"] += 1
            bucket["count"] += 1
        groups = [group for group in buckets.values() if int(group.get("count") or 0) > 0]
        return sorted(groups, key=lambda g: str(g.get("display_symbol") or ""))

    @staticmethod
    def _display_symbol_for(exchange_symbol: Any) -> str:
        raw = str(exchange_symbol or "").upper()
        return raw.split("-", 1)[0] if "-" in raw else raw

    @staticmethod
    def _string_or_none(value: Any) -> Optional[str]:
        return None if value in (None, "") else str(value)

    @classmethod
    def _trade_symbol_for(cls, base_symbol: Any) -> str:
        raw = str(base_symbol or "").strip().upper().replace("/", "-")
        if not raw:
            raise ValueError("ApeX order requires symbol")
        if "-" in raw:
            return raw
        return f"{raw}-USDT"

    @classmethod
    def _instrument_rules(cls, trade_symbol: str, config_v3: Mapping[str, Any]) -> dict[str, Decimal]:
        contract_config = cls._as_mapping(config_v3.get("contractConfig"))
        contracts = contract_config.get("perpetualContract")
        if not isinstance(contracts, list):
            raise ValueError("ApeX configs_v3 missing contractConfig.perpetualContract")
        match = None
        for contract in contracts:
            if not isinstance(contract, Mapping):
                continue
            if contract.get("symbol") == trade_symbol or contract.get("symbolDisplayName") == trade_symbol:
                match = contract
                break
        if not match:
            raise ValueError(f"ApeX instrument not found: {trade_symbol}")
        tick = cls._decimal_field(match, "tickSize")
        step = cls._decimal_field(match, "stepSize")
        minimum = cls._decimal_field(match, "minOrderSize", "minimumOrderSize", "minSize", "minOrderQty")
        if tick <= 0 or step <= 0 or minimum <= 0:
            raise ValueError(f"Invalid ApeX instrument rules for {trade_symbol}")
        return {"tickSize": tick, "stepSize": step, "minOrderSize": minimum}

    @classmethod
    def _normalize_price(cls, price: Any, rules: Mapping[str, Decimal]) -> str:
        return cls._format_decimal(cls._floor_to_increment(Decimal(str(price)), rules["tickSize"]))

    @classmethod
    def _normalize_size(cls, size: Any, rules: Mapping[str, Decimal]) -> str:
        normalized = cls._floor_to_increment(Decimal(str(size)), rules["stepSize"])
        if normalized < rules["minOrderSize"]:
            raise ValueError(f"ApeX order size {cls._format_decimal(normalized)} below minimum {cls._format_decimal(rules['minOrderSize'])}")
        return cls._format_decimal(normalized)

    @staticmethod
    def _floor_to_increment(value: Decimal, increment: Decimal) -> Decimal:
        if increment <= 0:
            raise ValueError("ApeX increment must be positive")
        return (value / increment).to_integral_value(rounding=ROUND_DOWN) * increment

    @staticmethod
    def _format_decimal(value: Decimal) -> str:
        text = format(value.normalize(), "f")
        if "." in text:
            text = text.rstrip("0").rstrip(".")
        return text or "0"

    @staticmethod
    def _decimal_field(source: Mapping[str, Any], *keys: str) -> Decimal:
        for key in keys:
            value = source.get(key)
            if value not in (None, ""):
                return Decimal(str(value))
        raise ValueError(f"Missing ApeX instrument rule: {'/'.join(keys)}")

    @classmethod
    def _market_price_for(cls, client: Any, symbol: str, side: str) -> Any:
        quote = None
        if hasattr(client, "depth"):
            quote = client.depth(symbol=symbol)
        elif hasattr(client, "ticker"):
            quote = client.ticker(symbol=symbol)
        data = cls._data_mapping(quote)
        if side == "BUY":
            ask = cls._first_value([data], "askPrice", "bestAsk", "ask", "a")
            if ask is not None:
                return ask
            asks = data.get("asks") if isinstance(data, Mapping) else None
            if isinstance(asks, list) and asks:
                first = asks[0]
                return first[0] if isinstance(first, list) else cls._as_mapping(first).get("price")
        bid = cls._first_value([data], "bidPrice", "bestBid", "bid", "b")
        if bid is not None:
            return bid
        bids = data.get("bids") if isinstance(data, Mapping) else None
        if isinstance(bids, list) and bids:
            first = bids[0]
            return first[0] if isinstance(first, list) else cls._as_mapping(first).get("price")
        raise ValueError("ApeX market order requires price and no bid/ask was available")

    @classmethod
    def _extract_order_id(cls, raw: Any) -> Any:
        top = cls._as_mapping(raw)
        data = cls._data_mapping(raw)
        return cls._first_value([data, top], "orderId", "id", "order_id", "clientOrderId")

    @classmethod
    def _verify_order_visible(cls, client: Any, order_id: Any) -> dict[str, Any]:
        if not order_id:
            return {"visible": False, "reason": "missing order id"}
        try:
            if hasattr(client, "get_order_v3"):
                raw = client.get_order_v3(id=order_id)
            elif hasattr(client, "_get"):
                from apexomni.constants import URL_SUFFIX
                raw = client._get(endpoint=URL_SUFFIX + "/v3/order", params={"id": order_id})
            else:
                return {"visible": True, "reason": "verification method unavailable"}
            data = cls._data_mapping(raw)
            visible_id = cls._first_value([data, cls._as_mapping(raw)], "orderId", "id", "order_id")
            return {"visible": bool(visible_id or data), "order_id": visible_id or order_id, "exchange_response": raw}
        except Exception as exc:
            return {"visible": False, "order_id": order_id, "reason": str(exc), "error_type": exc.__class__.__name__}

    def _resolve_credentials(self, alias: str) -> tuple[dict[str, str], Optional[str]]:
        account = self._normalize_alias(alias)
        if not account:
            return {}, "Missing ApeX account alias"
        env = combined_casefold_env()
        marker = self._raw_account_from_alias(account, env)
        if not marker:
            return {}, f"No ApeX account configured for alias: {alias}"
        missing: list[str] = []
        resolved: dict[str, str] = {}
        for suffix in APEX_REQUIRED_SUFFIXES:
            key = f"APEX_{marker}_{suffix}"
            found = env.get(key.lower())
            if not found or not found[1].strip():
                missing.append(key)
            else:
                resolved[suffix.lower()] = found[1].strip()
        if missing:
            return {}, "Missing ApeX credential variables: " + ", ".join(missing)
        return resolved, None

    @classmethod
    def _raw_account_from_alias(cls, alias: str, env: Mapping[str, tuple[str, str, str]]) -> Optional[str]:
        for actual_key, _value, _source in env.values():
            upper_key = actual_key.upper()
            if not upper_key.startswith("APEX_") or not upper_key.endswith("_ACCOUNTID"):
                continue
            raw_account = actual_key[len("APEX_") : -len("_ACCOUNTID")]
            if cls._normalize_alias(raw_account) == alias:
                return raw_account
        return None

    def _normalize_balance(self, raw_balance: Any, raw_account: Any) -> dict:
        balance_top = self._as_mapping(raw_balance)
        account_top = self._as_mapping(raw_account)
        balance_data = self._data_mapping(raw_balance)
        account_data = self._data_mapping(raw_account)
        self.last_response_shape = {
            "raw_balance": "data" if balance_data is not balance_top else "top-level",
            "raw_account": "data" if account_data is not account_top else "top-level",
        }

        sources = [balance_data, balance_top, account_data, account_top]
        nested_sources = sources + [
            self._as_mapping(src.get("account")) for src in sources
        ] + [
            self._as_mapping(src.get("contractAccount")) for src in sources
        ] + [
            self._as_mapping(src.get("crossMarginSummary")) for src in sources
        ] + [
            self._as_mapping(src.get("marginSummary")) for src in sources
        ]

        account_value = self._first_value(nested_sources, "accountValue", "totalEquityValue", "equity", "totalEquity", "accountEquity")
        margin_used = self._first_value(
            nested_sources,
            "totalMarginUsed",
            "initialMargin",
            "totalInitialMargin",
            "positionInitialMargin",
            "marginUsed",
        )
        withdrawable = self._first_value(nested_sources, "withdrawable", "withdrawableUsd", "availableToWithdraw", "availableBalance")
        total_position_value = self._first_value(nested_sources, "totalPositionValue", "positionValue", "totalNtlPos")
        positions = self._extract_positions(sources)

        margin_summary = {
            "accountValue": account_value,
            "totalMarginUsed": margin_used,
            "totalNtlPos": total_position_value,
        }
        return {
            "marginSummary": margin_summary,
            "crossMarginSummary": dict(margin_summary),
            "withdrawable": withdrawable,
            "assetPositions": positions,
            "positions": positions,
            "totalPositionValue": total_position_value,
            "raw_balance_shape": self.last_response_shape["raw_balance"],
            "raw_account_shape": self.last_response_shape["raw_account"],
        }

    @classmethod
    def _extract_positions(cls, sources: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
        raw_positions: Any = None
        for src in sources:
            for key in ("assetPositions", "positions", "openPositions", "positionList"):
                val = src.get(key)
                if isinstance(val, list):
                    raw_positions = val
                    break
            if raw_positions is not None:
                break
            contract = cls._as_mapping(src.get("contractAccount"))
            for key in ("assetPositions", "positions", "openPositions", "positionList"):
                val = contract.get(key)
                if isinstance(val, list):
                    raw_positions = val
                    break
            if raw_positions is not None:
                break
        if not isinstance(raw_positions, list):
            return []
        out: list[dict[str, Any]] = []
        for item in raw_positions:
            pos = cls._as_mapping(item.get("position")) if isinstance(item, Mapping) else {}
            if not pos and isinstance(item, Mapping):
                pos = cls._as_mapping(item)
            if not pos:
                continue
            normalized_pos = cls._normalize_position(pos)
            if normalized_pos:
                out.append({"position": normalized_pos})
        return out

    @classmethod
    def _normalize_position(cls, pos: Mapping[str, Any]) -> dict[str, Any]:
        coin = cls._first_value([pos], "coin", "symbol", "contract", "instrumentId")
        size = cls._first_value([pos], "szi", "size", "position", "qty", "amount")
        side = str(cls._first_value([pos], "side", "positionSide") or "").lower()
        if size is not None and side in {"short", "sell", "ask"}:
            try:
                size = str(-abs(float(size)))
            except Exception:
                pass
        entry = cls._first_value([pos], "entryPx", "entryPrice", "avgEntryPrice", "averageEntryPrice")
        pnl = cls._first_value([pos], "unrealizedPnl", "unrealizedPnlUsd", "pnl", "unrealizedProfit")
        liq = cls._first_value([pos], "liquidationPx", "liqPx", "liquidationPrice")
        normalized = {"coin": coin, "szi": size, "entryPx": entry, "unrealizedPnl": pnl, "liquidationPx": liq}
        return {k: v for k, v in normalized.items() if v is not None}

    @staticmethod
    def _normalize_alias(account: str) -> str:
        return str(account or "").strip().lower()

    @staticmethod
    def _has_complete_credentials(raw_account: str, env: Mapping[str, tuple[str, str, str]]) -> bool:
        for suffix in APEX_REQUIRED_SUFFIXES:
            key = f"APEX_{raw_account}_{suffix}".lower()
            found = env.get(key)
            if not found or not found[1].strip():
                return False
        return True

    @staticmethod
    def _as_mapping(value: Any) -> Mapping[str, Any]:
        return value if isinstance(value, Mapping) else {}

    @classmethod
    def _data_mapping(cls, value: Any) -> Mapping[str, Any]:
        top = cls._as_mapping(value)
        data = top.get("data")
        return data if isinstance(data, Mapping) else top

    @staticmethod
    def _first_value(sources: list[Mapping[str, Any]], *keys: str) -> Any:
        for source in sources:
            if not isinstance(source, Mapping):
                continue
            for key in keys:
                value = source.get(key)
                if value not in (None, ""):
                    return value
        return None
