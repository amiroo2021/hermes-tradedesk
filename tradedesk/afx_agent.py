"""AFX exchange-specific executor.

AFX support is intentionally routed through TradeDesk like every other exchange.
Telegram never imports or calls this module directly.
"""
from __future__ import annotations

import json
import logging
import math
import os
import re
import time
import urllib.parse
import urllib.request
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP, InvalidOperation
from pathlib import Path
from typing import Any, Mapping, Optional

from .account_discovery import discover_accounts
from .request_utils import _request_field

# Telegram never imports or calls this module directly.
AFX_MAINNET_API_URL = "https://api.afx.xyz"
AFX_DEFAULT_MARKET_SLIPPAGE_PCT = "0.01"  # AFX JS SDK README: decimal ratio string; "0.01" means 1%.
SUPPORTED_OPERATIONS = {"order", "batch_orders", "positions", "set_tp", "set_sl", "balance", "open_orders", "cancel_orders"}
logger = logging.getLogger(__name__)


def _execution_result(request: Mapping[str, Any], *, success: bool, error: Optional[str] = None, **extra: Any) -> dict:
    result = {
        "success": success,
        "exchange": "afx",
        "operation": request.get("operation"),
        "parent_operation": request.get("parent_operation"),
        "account": request.get("account"),
    }
    if error:
        result["error"] = error
    result.update(extra)
    return result


def _hermes_env_path() -> Path:
    home = os.getenv("HERMES_HOME")
    return (Path(home).expanduser() if home else Path.home() / ".hermes") / ".env"


def _strip_dotenv_value(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def _dotenv_casefold_map() -> dict[str, tuple[str, str]]:
    path = _hermes_env_path()
    out: dict[str, tuple[str, str]] = {}
    if not path.exists():
        return out
    for raw_line in path.read_text(errors="ignore").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key.lower().startswith("export "):
            key = key[7:].strip()
        if key and value.strip():
            out[key.lower()] = (key, _strip_dotenv_value(value).strip())
    return out


def _combined_casefold_env() -> dict[str, tuple[str, str, str]]:
    out: dict[str, tuple[str, str, str]] = {}
    for env_key, env_value in os.environ.items():
        if env_value and env_value.strip():
            out[env_key.lower()] = (env_key, env_value.strip(), "environment")
    for lower_key, (actual_key, value) in _dotenv_casefold_map().items():
        if lower_key not in out:
            out[lower_key] = (actual_key, value, "dotenv")
    return out


def _account_segment(account: Optional[str]) -> Optional[str]:
    raw = str(account or "").strip()
    if not raw:
        return None
    normalized = re.sub(r"[^A-Za-z0-9]+", "_", raw).strip("_").upper()
    return normalized or None


def _candidate_names(account: Optional[str], kind: str) -> list[str]:
    segment = _account_segment(account)
    if segment:
        return [f"AFX_{segment}_{kind}"]
    return [f"AFX_{kind}"]


def _lookup_case_insensitive(names: list[str]) -> tuple[Optional[str], Optional[str], list[str]]:
    available = _combined_casefold_env()
    for name in names:
        found = available.get(name.lower())
        if found:
            actual_key, value, _source = found
            return value, actual_key, names
    return None, None, names


def _resolve_credentials(account: Optional[str]) -> tuple[Optional[str], Optional[str], Optional[str], Optional[str], list[str]]:
    wallet, wallet_key, wallet_searched = _lookup_case_insensitive(_candidate_names(account, "WALLET"))
    agent_key, agent_key_name, agent_searched = _lookup_case_insensitive(_candidate_names(account, "AGENT_PRIVATE_KEY"))
    return wallet, agent_key, wallet_key, agent_key_name, wallet_searched + agent_searched


def _credentialed_accounts() -> list[str]:
    available = _combined_casefold_env()
    pattern = re.compile(r"^AFX_(.+)_(WALLET|AGENT_PRIVATE_KEY)$", re.IGNORECASE)
    accounts: dict[str, set[str]] = {}
    display: dict[str, str] = {}
    for actual_key, _value, _source in available.values():
        match = pattern.fullmatch(actual_key)
        if not match:
            continue
        segment = match.group(1)
        kind = match.group(2).upper()
        account = re.sub(r"_+", "_", segment).strip("_").lower()
        if not account:
            continue
        display.setdefault(account, account)
        accounts.setdefault(account, set()).add(kind)
    return [display[name] for name in sorted(accounts) if {"WALLET", "AGENT_PRIVATE_KEY"}.issubset(accounts[name])]


class AfxAgent:
    """Execute normalized AFX requests.

    Position reads use AFX Info API directly until the official SDK is installed
    in this runtime. Trading actions remain explicit clean errors rather than
    guessed SDK calls.
    """

    def __init__(self, *, base_url: str = AFX_MAINNET_API_URL, info_client: Any = None, exchange_client: Any = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.info_client = info_client
        self.exchange_client = exchange_client

    def list_accounts(self) -> dict:
        accounts = discover_accounts("afx")
        return {
            "success": True,
            "exchange": "afx",
            "accounts": accounts,
            "message": f"Found {len(accounts)} AFX configured account(s).",
        }

    def execute(self, request: Mapping[str, Any]) -> dict:
        operation = str(request.get("operation") or "")
        if operation not in SUPPORTED_OPERATIONS:
            return _execution_result(request, success=False, error=f"Unsupported AFX operation: {operation}")
        try:
            if operation == "order":
                return self._order(request)
            if operation == "batch_orders":
                return self._batch_orders_one_by_one(request)
            if operation == "positions":
                return self._positions(request)
            if operation == "balance":
                return self._balance(request)
            if operation == "open_orders":
                return self._open_orders(request)
            if operation == "cancel_orders":
                return self._cancel_orders(request)
            if operation in {"set_tp", "set_sl"}:
                return self._set_tpsl(request, operation)
        except Exception as exc:
            return _execution_result(request, success=False, error=str(exc), error_type=exc.__class__.__name__)
        return _execution_result(request, success=False, error="Unhandled AFX operation")

    def _credential_context(self, request: Mapping[str, Any]) -> tuple[Optional[str], Optional[str], dict]:
        account = str(request.get("account") or "") or None
        wallet, agent_key, wallet_var, agent_var, searched = _resolve_credentials(account)
        context = {
            "wallet": wallet_var,
            "agent_private_key": agent_var,
            "searched": searched,
            "env_path": str(_hermes_env_path()),
        }
        return wallet, agent_key, context

    def _require_wallet(self, request: Mapping[str, Any]) -> tuple[Optional[str], dict, Optional[dict]]:
        wallet, _agent_key, credential_context = self._credential_context(request)
        if not wallet:
            error = _execution_result(
                request,
                success=False,
                error=(
                    f"Missing AFX wallet for account {request.get('account')!r}. "
                    f"Searched environment variables: {', '.join(credential_context['searched'])}. "
                    f"Credential file checked: {credential_context['env_path']}"
                ),
                credential_variables=credential_context,
            )
            return None, credential_context, error
        return wallet, credential_context, None

    def _require_agent_key(self, request: Mapping[str, Any]) -> tuple[Optional[str], Optional[str], dict, Optional[dict]]:
        wallet, agent_key, credential_context = self._credential_context(request)
        if not wallet or not agent_key:
            error = _execution_result(
                request,
                success=False,
                error=(
                    f"Missing AFX wallet or agent private key for account {request.get('account')!r}. "
                    f"Searched environment variables: {', '.join(credential_context['searched'])}. "
                    f"Credential file checked: {credential_context['env_path']}"
                ),
                credential_variables=credential_context,
            )
            return wallet, agent_key, credential_context, error
        return wallet, agent_key, credential_context, None

    def _positions(self, request: Mapping[str, Any]) -> dict:
        wallet, credential_context, error = self._require_wallet(request)
        if error:
            return error
        if self.info_client is not None:
            if not hasattr(self.info_client, "get_positions"):
                return _execution_result(
                    request,
                    success=False,
                    error="AFX info client does not support get_positions",
                    credential_variables=credential_context,
                )
            raw = self.info_client.get_positions(wallet)
            products = self._products_response()
        else:
            raw = self._get_json("/info/position/list", {"userAddr": wallet, "includeZero": "false"})
            products = self._products_response()
        # AFX TP/SL trigger orders are not part of the positions payload;
        # they live as attached trigger orders on the orders endpoint.
        # Fetch the full open-orders list (status=None to include
        # ORDER_STATUS_UNTRIGGERED) and pass it to the normalizer so the
        # resulting position dicts carry take_profit / stop_loss.
        try:
            raw_orders = self._get_open_orders(wallet)
        except Exception as exc:
            raw_orders = []
            logger.warning(
                "AFX orders fetch failed for positions enrichment on %s: %s",
                wallet, exc,
            )
        positions = self._normalize_positions(raw, products, raw_orders=raw_orders)
        return _execution_result(
            request,
            success=True,
            positions=positions,
            credential_variables=credential_context,
            exchange_response=raw,
            raw_response=raw,
        )

    def _order(self, request: Mapping[str, Any]) -> dict:
        _wallet, _agent_key, credential_context, error = self._require_agent_key(request)
        if error:
            return error
        child = dict(request.get("child_order") or {})
        if not child and request.get("child_orders"):
            child = dict(request.get("child_orders", [])[0])
        if not child:
            return _execution_result(request, success=False, error="AFX order requires child_order", credential_variables=credential_context)
        sdk_payload = self._child_to_order_payload(child)
        exchange_client = self.exchange_client or self._sdk_exchange_client(request)
        logger.error("AFX LIVE ORDER PAYLOAD operation=%s payload=%r", request.get("operation"), sdk_payload)
        raw = exchange_client.place_order(**sdk_payload)
        child_success, verify = self._verify_order_acceptance(request, sdk_payload, raw)
        return _execution_result(
            request,
            success=child_success,
            error=None if child_success else "AFX order submission was not verified as an open order",
            child_order=child,
            sdk_payload=sdk_payload,
            credential_variables=credential_context,
            exchange_response=raw,
            raw_response=raw,
            verification=verify,
        )

    def _batch_orders_one_by_one(self, request: Mapping[str, Any]) -> dict:
        _wallet, _agent_key, credential_context, error = self._require_agent_key(request)
        if error:
            return error
        child_orders = [dict(child) for child in request.get("child_orders", [])]
        if not child_orders:
            return _execution_result(request, success=False, error="AFX batch_orders requires child_orders", credential_variables=credential_context)

        exchange_client = self.exchange_client or self._sdk_exchange_client(request)
        child_results = []
        raw_results = []
        sdk_payloads = []
        all_success = True
        for child in child_orders:
            try:
                sdk_payload = self._child_to_order_payload(child)
                logger.error("AFX LIVE ORDER PAYLOAD operation=%s payload=%r", request.get("parent_operation") or request.get("operation"), sdk_payload)
                raw = exchange_client.place_order(**sdk_payload)
                sdk_payloads.append(sdk_payload)
                raw_results.append(raw)
                child_success, verify = self._verify_order_acceptance(request, sdk_payload, raw)
                all_success = all_success and child_success
                child_result = {
                    "child_id": child.get("child_id"),
                    "symbol": child.get("symbol"),
                    "side": child.get("side"),
                    "order_type": child.get("order_type"),
                    "size": child.get("size"),
                    "price": child.get("price"),
                    "success": child_success,
                    "child_order": child,
                    "sdk_payload": sdk_payload,
                    "exchange_response": raw,
                    "verification": verify,
                }
                if not child_success:
                    child_result["error"] = verify.get("reason") or "AFX child order not verified as open"
                child_results.append(child_result)
            except Exception as exc:
                all_success = False
                child_results.append({
                    "child_id": child.get("child_id"),
                    "symbol": child.get("symbol"),
                    "side": child.get("side"),
                    "order_type": child.get("order_type"),
                    "size": child.get("size"),
                    "price": child.get("price"),
                    "success": False,
                    "child_order": child,
                    "error": str(exc),
                    "error_type": exc.__class__.__name__,
                })
                # Continue submitting remaining ladder children; AFX has no batch endpoint.
                continue

        return _execution_result(
            request,
            success=all_success,
            error=None if all_success else "One or more AFX ladder child orders were submitted but not verified open",
            normalized_request=dict(request),
            structured_request=dict(request.get("structured_request") or {}),
            submission_mode="one_by_one",
            sdk_payload={"method": "place_order", "mode": "one_by_one", "order_requests": sdk_payloads},
            credential_variables=credential_context,
            exchange_response=raw_results,
            raw_response=raw_results,
            child_results=child_results,
        )

    def _balance(self, request: Mapping[str, Any]) -> dict:
        wallet, credential_context, error = self._require_wallet(request)
        if error:
            return error
        if self.info_client is not None and hasattr(self.info_client, "get_wallet"):
            raw = self.info_client.get_wallet(wallet, include_zero=False)
        else:
            raw = self._get_json("/info/account/wallet", {"userAddr": wallet, "includeZero": "false"})
        return _execution_result(
            request,
            success=True,
            credential_variables=credential_context,
            exchange_response=raw,
            raw_response=raw,
            wallets=(raw.get("data") if isinstance(raw, Mapping) else raw),
        )

    def _open_orders(self, request: Mapping[str, Any]) -> dict:
        wallet, credential_context, error = self._require_wallet(request)
        if error:
            return error
        symbol_code = self._symbol_code_from_request(request) if request.get("symbol") and str(request.get("symbol")).lower() not in {"all", "all symbols"} else None
        raw = self._get_open_orders(wallet, symbol_code=symbol_code)
        products = self._products_response()
        orders = [self._normalize_afx_order(order, products) for order in self._orders_from_response(raw)]
        active_orders = [order for order in orders if order.get("is_active")]
        order_summary = self._summarize_orders_by_symbol_side(active_orders)
        return _execution_result(
            request,
            success=True,
            credential_variables=credential_context,
            exchange_response=raw,
            raw_response=raw,
            orders=active_orders,
            order_summary=order_summary,
            open_order_count=len(active_orders),
        )

    def _cancel_orders(self, request: Mapping[str, Any]) -> dict:
        _wallet, _agent_key, credential_context, error = self._require_agent_key(request)
        if error:
            return error
        exchange_client = self.exchange_client or self._sdk_exchange_client(request)
        order_id = request.get("order_id") or request.get("ord_id") or request.get("oid")
        symbol = request.get("symbol")
        symbol_code = self._symbol_code_from_request(request) if symbol and str(symbol).lower() not in {"all", "all symbols"} else None
        if order_id:
            if symbol_code is None:
                return _execution_result(request, success=False, error="AFX cancel by order id requires symbol", credential_variables=credential_context)
            payload = {"symbol_code": symbol_code, "ord_id": str(order_id)}
            raw = exchange_client.cancel_order(**payload)
            return _execution_result(request, success=True, sdk_payload=payload, credential_variables=credential_context, exchange_response=raw, raw_response=raw, canceled_count=1)
        if symbol_code is not None:
            scoped_side = str(request.get("side") or "").lower()
            scoped_type = str(request.get("order_type") or "").lower()
            if scoped_side in {"buy", "sell"} or scoped_type in {"limit", "market"}:
                raw_orders = self._get_open_orders(str(_wallet), symbol_code=symbol_code)
                products = self._products_response()
                normalized_orders = [self._normalize_afx_order(order, products) for order in self._orders_from_response(raw_orders)]
                matches = self._filter_normalized_cancel_orders(normalized_orders, symbol_code, request)
                cancel_payloads = []
                raw_results = []
                for order in matches:
                    oid = self._order_id(order)
                    if not oid:
                        continue
                    payload = {"symbol_code": symbol_code, "ord_id": oid}
                    cancel_payloads.append(payload)
                    raw_results.append(exchange_client.cancel_order(**payload))
                raw_response = raw_results[0] if len(raw_results) == 1 else raw_results
                return _execution_result(
                    request,
                    success=True,
                    sdk_payload={"cancel_orders": cancel_payloads},
                    credential_variables=credential_context,
                    exchange_response=raw_response,
                    raw_response=raw_response,
                    canceled_count=len(cancel_payloads),
                    matching_orders=[self._safe_order_summary(order) for order in matches],
                )
            cancel_payloads = self._cancel_all_payloads_for_symbol(symbol_code, request)
            raw_results = [exchange_client.cancel_all(**payload) for payload in cancel_payloads]
            raw_response = raw_results[0] if len(raw_results) == 1 else raw_results
            return _execution_result(
                request,
                success=True,
                sdk_payload={"cancel_all": cancel_payloads},
                credential_variables=credential_context,
                exchange_response=raw_response,
                raw_response=raw_response,
                canceled_count=len(cancel_payloads),
            )
        wallet = _wallet
        raw_orders = self._get_open_orders(str(wallet))
        cancel_payloads = []
        for code in self._active_order_symbol_codes(raw_orders):
            cancel_payloads.extend(self._cancel_all_payloads_for_symbol(code, request))
        raw_results = [exchange_client.cancel_all(**payload) for payload in cancel_payloads]
        return _execution_result(
            request,
            success=True,
            sdk_payload={"cancel_all": cancel_payloads},
            credential_variables=credential_context,
            exchange_response=raw_results,
            raw_response=raw_results,
            canceled_count=len(cancel_payloads),
        )

    def _set_tpsl(self, request: Mapping[str, Any], operation: str) -> dict:
        wallet, _agent_key, credential_context, error = self._require_agent_key(request)
        if error:
            return error
        symbol_code = self._symbol_code_from_request(request)
        price = self._to_float(_request_field(request, "price")) or 0.0
        kind = "tp" if operation == "set_tp" else "sl"
        label = "take profit" if kind == "tp" else "stop loss"
        reduce_code = 2 if kind == "tp" else 3
        reduce_option = "TP_FROM_POSITION" if kind == "tp" else "SL_FROM_POSITION"
        position_raw = _request_field(request, "position")
        position = position_raw if isinstance(position_raw, Mapping) else {}
        side = str((position.get("side") if isinstance(position, Mapping) else None) or _request_field(request, "side") or "").lower()
        close_side_code = 2 if side == "long" else 1 if side == "short" else None
        symbol_name = str(_request_field(request, "symbol") or (position.get("symbol") if isinstance(position, Mapping) else None) or "").upper()
        position_line = f"{symbol_name} {str(side or '').title()}".strip()

        existing = self._matching_tpsl_orders(str(wallet), symbol_code, reduce_code, side_code=close_side_code)
        exchange_client = self.exchange_client or self._sdk_exchange_client(request)
        cancel_results, cancel_payloads = self._cancel_tpsl_orders(exchange_client, symbol_code, existing)
        failed_cancel = next((raw for raw in cancel_results if not self._raw_success(raw)), None)
        if failed_cancel is not None:
            return _execution_result(
                request,
                success=False,
                error=self._raw_error(failed_cancel) or f"AFX {label} cancellation failed",
                sdk_payload={"cancel_requests": cancel_payloads},
                credential_variables=credential_context,
                exchange_response=cancel_results,
                raw_response=cancel_results,
                canceled_count=len(cancel_payloads),
            )

        if cancel_payloads:
            cleared, remaining = self._verify_no_matching_tpsl(str(wallet), symbol_code, reduce_code, side_code=close_side_code)
            if not cleared:
                return _execution_result(
                    request,
                    success=False,
                    error=f"AFX {label} cancellation was accepted but matching trigger orders are still open",
                    sdk_payload={"cancel_requests": cancel_payloads},
                    credential_variables=credential_context,
                    exchange_response=cancel_results,
                    raw_response=cancel_results,
                    canceled_count=len(cancel_payloads),
                    remaining_orders=[self._safe_order_summary(order) for order in remaining],
                )

        if price == 0:
            if not existing:
                return _execution_result(
                    request,
                    success=True,
                    sdk_payload={"cancel_requests": []},
                    credential_variables=credential_context,
                    exchange_response=[],
                    raw_response=[],
                    canceled_count=0,
                    message=(
                        f"✅ AFX {label} unchanged — {request.get('account')}\n\n"
                        f"{position_line}\nNo existing {'TP' if kind == 'tp' else 'SL'} to remove."
                    ),
                )
            return _execution_result(
                request,
                success=True,
                sdk_payload={"cancel_requests": cancel_payloads},
                credential_variables=credential_context,
                exchange_response=cancel_results,
                raw_response=cancel_results,
                canceled_count=len(cancel_payloads),
                message=(
                    f"✅ AFX {label} removed — {request.get('account')}\n\n"
                    f"{position_line}\nCancelled {'TP' if kind == 'tp' else 'SL'} orders: {len(cancel_payloads)}"
                ),
            )

        size = position.get("size") or request.get("size")
        is_buy = side == "short"
        child = {
            "symbol": request.get("symbol") or position.get("symbol"),
            "is_buy": is_buy,
            "side": "buy" if is_buy else "sell",
            "order_type": "market",
            "size": size,
            "price": 0,
            "reduce_only": True,
        }
        payload = self._child_to_order_payload(child)
        product = self._product_for_child(child, self._products_response())
        trigger_px = self._format_price_for_product(price, product)
        # AFX rejects TAKE_PROFIT_MARKET / STOP_MARKET through place_order with
        # "Invalid order type".  The SDK exposes TP/SL as MARKET + trigger fields
        # plus TP_FROM_POSITION / SL_FROM_POSITION reduce-only options.
        # AFX also rejects MARKET orders unless TIF is IOC or FOK.
        payload.update({
            "ord_type": "MARKET",
            "tif": "IOC",
            "px": "0",
            "reduce_only_option": reduce_option,
            "trigger_px": trigger_px,
            "trigger_type": "MARK_PRICE",
        })
        logger.error("AFX LIVE ORDER PAYLOAD operation=%s payload=%r", operation, payload)
        raw = exchange_client.place_order(**payload)
        success = self._raw_success(raw)
        if not success:
            return _execution_result(
                request,
                success=False,
                error=self._raw_error(raw) or f"AFX {label} submission failed",
                child_order=child,
                sdk_payload=payload,
                replace_cancel_requests=cancel_payloads,
                credential_variables=credential_context,
                exchange_response=raw,
                raw_response=raw,
                canceled_count=len(cancel_payloads),
            )

        verified, matches = self._verify_exactly_one_matching_tpsl(
            str(wallet), symbol_code, reduce_code, side_code=close_side_code, trigger_px=trigger_px
        )
        if not verified:
            return _execution_result(
                request,
                success=False,
                error=f"AFX {label} submission accepted but exactly one matching trigger order was not verified open",
                child_order=child,
                sdk_payload=payload,
                replace_cancel_requests=cancel_payloads,
                credential_variables=credential_context,
                exchange_response=raw,
                raw_response=raw,
                canceled_count=len(cancel_payloads),
                matching_orders=[self._safe_order_summary(order) for order in matches],
            )

        new_order_id = self._order_id(matches[0])
        old_triggers = [str(order.get("trigger_price") or order.get("triggerPx") or order.get("trigger_px") or "") for order in existing]
        return _execution_result(
            request,
            success=True,
            child_order=child,
            sdk_payload=payload,
            replace_cancel_requests=cancel_payloads,
            credential_variables=credential_context,
            exchange_response=raw,
            raw_response=raw,
            canceled_count=len(cancel_payloads),
            new_order_id=new_order_id,
            trigger_px=trigger_px,
            matching_order=self._safe_order_summary(matches[0]),
            message=(
                f"✅ AFX {label} updated — {request.get('account')}\n\n"
                f"{position_line}\n"
                + (f"Old trigger: {', '.join(t for t in old_triggers if t) or 'n/a'}\n" if existing else "")
                + f"New trigger: {trigger_px}\n"
                f"Cancelled old orders: {len(cancel_payloads)}\n"
                f"New order ID: {new_order_id or 'unknown'}"
            ),
        )

    def _products_response(self) -> Any:
        if self.info_client is not None and hasattr(self.info_client, "get_products"):
            return self.info_client.get_products()
        return self._get_json("/info/public/product-meta", {})

    def _symbol_code_from_request(self, request: Mapping[str, Any]) -> int:
        symbol = _request_field(request, "symbol")
        if not symbol and isinstance(_request_field(request, "position"), Mapping):
            symbol = (_request_field(request, "position") or {}).get("symbol")
        return self._symbol_code_for_child({"symbol": symbol}, self._products_response())

    def _get_open_orders(self, wallet: str, *, symbol_code: Optional[int] = None) -> Any:
        # AFX TP/SL trigger orders are returned by /info/order/states, but not
        # when filtered to status=NEW. Live TP orders use
        # status="ORDER_STATUS_UNTRIGGERED", so fetch active states without the
        # NEW-only filter and let local normalization decide what is open.
        if self.info_client is not None and hasattr(self.info_client, "get_orders"):
            return self.info_client.get_orders(wallet, symbol=symbol_code, status=None)
        params: dict[str, Any] = {"userAddr": wallet, "pageSize": 500}
        if symbol_code is not None:
            params["symbol"] = symbol_code
        return self._get_json("/info/order/states", params)

    def _verify_order_acceptance(self, request: Mapping[str, Any], payload: Mapping[str, Any], raw: Any) -> tuple[bool, dict]:
        """Confirm AFX accepted an order and, for normal limit orders, that it appears open.

        AFX can return `{code: 0, txMsg: submitted to consensus}` before a limit
        order is visible in the order book.  A green Telegram checkmark should
        mean more than tx submission, so normal GTC limit orders are verified
        against `/info/order/states` after a short wait. Market/trigger orders can
        fill/fire/cancel immediately, so for those we keep tx-level acceptance.
        """
        tx_ok = self._raw_success(raw)
        tx_hash = self._extract_tx_hash(raw)
        verify = {"tx_success": tx_ok, "tx_hash": tx_hash, "checked_open_orders": False}
        if not tx_ok:
            verify["reason"] = self._raw_error(raw) or "AFX submission failed"
            return False, verify

        if str(payload.get("ord_type") or "").upper() != "LIMIT" or str(payload.get("tif") or "").upper() != "GTC":
            verify["reason"] = "non-GTC-limit order: transaction accepted"
            return True, verify

        wallet, _agent_key, _credential_context = self._credential_context(request)
        if not wallet:
            verify["reason"] = "wallet unavailable for open-order verification"
            return False, verify

        symbol_code = self._to_int(payload.get("symbol_code"))
        expected_side = str(payload.get("side") or "").upper()
        expected_price = self._to_float(payload.get("px"))
        expected_qty = self._to_float(payload.get("qty"))
        attempts = []
        for attempt in range(3):
            if attempt:
                time.sleep(1.0)
            raw_open = self._get_open_orders(str(wallet), symbol_code=symbol_code)
            orders = self._orders_from_response(raw_open)
            matched = self._find_matching_open_order(orders, symbol_code, expected_side, expected_price, expected_qty)
            attempts.append({"attempt": attempt + 1, "open_count": len(orders), "matched": bool(matched)})
            if matched:
                verify.update({
                    "checked_open_orders": True,
                    "open_order_verified": True,
                    "attempts": attempts,
                    "matched_order": self._safe_order_summary(matched),
                })
                return True, verify
        verify.update({
            "checked_open_orders": True,
            "open_order_verified": False,
            "attempts": attempts,
            "reason": "transaction submitted but matching open order was not found",
        })
        return False, verify

    @staticmethod
    def _cancel_all_payloads_for_symbol(symbol_code: int, request: Mapping[str, Any]) -> list[dict]:
        order_type = str(request.get("order_type") or "all").lower().replace(" ", "_")
        include_regular = order_type in {"", "all", "both", "limit", "market", "regular"}
        include_conditional = order_type in {"all", "both", "conditional", "trigger", "tp", "sl", "tpsl", "take_profit", "stop_loss"}
        payloads = []
        if include_regular:
            payloads.append({"symbol_code": symbol_code, "conditional": False})
        if include_conditional:
            payloads.append({"symbol_code": symbol_code, "conditional": True})
        return payloads or [{"symbol_code": symbol_code, "conditional": False}]

    @classmethod
    def _orders_from_response(cls, raw: Any) -> list[dict]:
        data = raw.get("data") if isinstance(raw, Mapping) else raw
        if isinstance(data, Mapping):
            orders = data.get("items") or data.get("orders") or data.get("rows") or []
        else:
            orders = data
        return [dict(order) for order in orders] if isinstance(orders, list) else []

    @staticmethod
    def _extract_tx_hash(raw: Any) -> Optional[str]:
        if not isinstance(raw, Mapping):
            return None
        data = raw.get("data")
        if isinstance(data, Mapping):
            tx_hash = data.get("txHash") or data.get("hash")
            return str(tx_hash) if tx_hash else None
        return None

    @staticmethod
    def _raw_error(raw: Any) -> Optional[str]:
        if not isinstance(raw, Mapping):
            return None
        data = raw.get("data")
        parts = [raw.get("message"), raw.get("error")]
        if isinstance(data, Mapping):
            parts.extend([data.get("txMsg"), data.get("error"), data.get("reason")])
        return "; ".join(str(part) for part in parts if part not in (None, "", "success")) or None

    @classmethod
    def _find_matching_open_order(
        cls,
        orders: list[dict],
        symbol_code: Optional[int],
        expected_side: str,
        expected_price: Optional[float],
        expected_qty: Optional[float],
    ) -> Optional[dict]:
        expected_side_code = {"BUY": 1, "SELL": 2}.get(expected_side)
        for order in orders:
            try:
                order_symbol_raw = order.get("symbol")
                if symbol_code is not None and order_symbol_raw is not None and int(order_symbol_raw) != int(symbol_code):
                    continue
            except Exception:
                continue
            if str(order.get("status", "NEW")).upper() not in {"NEW", "PARTIALLY_FILLED"}:
                continue
            try:
                order_side = int(order.get("side"))
            except Exception:
                order_side = None
            if expected_side_code is not None and order_side is not None and order_side != expected_side_code:
                continue
            order_price = cls._to_float(order.get("price"))
            order_qty = cls._to_float(order.get("qty") or order.get("leaveQty"))
            if expected_price is not None and order_price is not None and abs(order_price - expected_price) > max(1e-8, abs(expected_price) * 1e-9):
                continue
            if expected_qty is not None and order_qty is not None and abs(order_qty - expected_qty) > max(1e-8, abs(expected_qty) * 1e-6):
                continue
            return order
        return None

    @staticmethod
    def _safe_order_summary(order: Mapping[str, Any]) -> dict:
        return {key: order.get(key) for key in ("ordId", "order_id", "clOrdId", "symbol", "symbol_code", "side", "side_code", "type", "order_type", "price", "qty", "leaveQty", "status", "reduceOnly", "reduce_only_option", "is_reduce_only", "triggerPx", "trigger_price", "triggerType", "trigger_type", "conditionalOrderTriggerType", "timeInForce")}

    @classmethod
    def _active_order_symbol_codes(cls, raw: Any) -> list[int]:
        orders = cls._orders_from_response(raw)
        if not orders:
            return []
        codes = []
        for order in orders:
            if not isinstance(order, Mapping):
                continue
            normalized = cls._normalize_afx_order(order, {})
            if not normalized.get("is_active"):
                continue
            code = normalized.get("symbol_code")
            if isinstance(code, int) and code not in codes:
                codes.append(code)
        return codes

    @classmethod
    def _normalize_afx_order(cls, order: Mapping[str, Any], products: Any) -> dict:
        symbol_code = cls._to_int(order.get("symbol") or order.get("symbolCode") or order.get("symbol_code"))
        product = cls._product_by_code(products).get(symbol_code or -1, {}) if products else {}
        symbol = str(product.get("symbol") or order.get("symbolName") or order.get("symbol_name") or symbol_code or "")
        side_raw = order.get("side") or order.get("ordSide") or order.get("orderSide")
        side_code = cls._to_int(side_raw)
        side = {1: "BUY", 2: "SELL"}.get(side_code, str(side_raw or "").upper())
        type_raw = order.get("type") or order.get("ordType") or order.get("orderType")
        type_code = cls._to_int(type_raw)
        order_type = {1: "LIMIT", 2: "MARKET"}.get(type_code, str(type_raw or "").upper())
        reduce_raw = order.get("reduceOnlyOption") or order.get("reduce_only_option") or order.get("reduceOnly")
        reduce_code = cls._to_int(reduce_raw)
        reduce_option = {0: None, 1: "REDUCE_ONLY", 2: "TP_FROM_POSITION", 3: "SL_FROM_POSITION"}.get(
            reduce_code, str(reduce_raw or "").upper() or None
        )
        trigger_type_raw = order.get("triggerType") or order.get("tpslTriggerType") or order.get("conditionalOrderTriggerType")
        trigger_type_code = cls._to_int(trigger_type_raw)
        trigger_type = {0: None, 1: "LAST_PRICE", 2: "MARK_PRICE", 3: "INDEX_PRICE"}.get(
            trigger_type_code, str(trigger_type_raw or "").upper() or None
        )
        trigger_price_raw = order.get("triggerPx") or order.get("triggerPrice") or order.get("trigger_px")
        trigger_price = cls._decimal_string_or_none(trigger_price_raw)
        status = str(order.get("status") or order.get("orderStatus") or "").upper()
        is_active = status in {"", "NEW", "OPEN", "ACTIVE", "PENDING", "PARTIALLY_FILLED", "ORDER_STATUS_UNTRIGGERED"}
        is_trigger = bool(trigger_price and trigger_price != "0") or bool(trigger_type) or reduce_option in {"TP_FROM_POSITION", "SL_FROM_POSITION"}
        return {
            "order_id": cls._order_id(order),
            "symbol": symbol,
            "asset": str(product.get("baseCurrency") or product.get("baseAsset") or symbol).replace("USDC", ""),
            "symbol_code": symbol_code,
            "side": side,
            "side_code": side_code,
            "status": status or "OPEN",
            "is_active": is_active,
            "order_type": order_type,
            "order_type_code": type_code,
            "trigger_price": trigger_price,
            "trigger_type": trigger_type,
            "trigger_type_code": trigger_type_code,
            "reduce_only_option": reduce_option,
            "reduce_only_code": reduce_code,
            "is_reduce_only": reduce_code not in (None, 0) or reduce_option in {"REDUCE_ONLY", "TP_FROM_POSITION", "SL_FROM_POSITION"},
            "raw_order": dict(order),
        }

    @staticmethod
    def _decimal_string_or_none(value: Any) -> Optional[str]:
        if value in (None, ""):
            return None
        try:
            dec = Decimal(str(value))
        except (InvalidOperation, ValueError, TypeError):
            return str(value)
        normalized = dec.normalize()
        if normalized == normalized.to_integral_value():
            return format(normalized, "f")
        return format(normalized, "f").rstrip("0").rstrip(".")

    @staticmethod
    def _trigger_prices_equal(left: Any, right: Any) -> bool:
        try:
            return Decimal(str(left)) == Decimal(str(right))
        except (InvalidOperation, ValueError, TypeError):
            return str(left) == str(right)

    def _log_safe_order_response(self, context: str, raw: Any, products: Any) -> None:
        orders = self._orders_from_response(raw)
        summaries = []
        for order in orders:
            if not isinstance(order, Mapping):
                continue
            normalized = self._normalize_afx_order(order, products)
            raw_safe = self._safe_order_summary(order)
            summaries.append({
                "normalized": {k: v for k, v in normalized.items() if k != "raw_order"},
                "raw_safe": raw_safe,
            })
        logger.error("AFX ORDER READ %s count=%s orders=%r", context, len(summaries), summaries)

    @staticmethod
    def _summarize_orders_by_symbol_side(orders: list[dict]) -> list[dict]:
        buckets: dict[str, dict] = {}
        for order in orders:
            if not isinstance(order, Mapping) or not order.get("is_active"):
                continue
            label = str(order.get("asset") or order.get("symbol") or order.get("symbol_code") or "UNKNOWN")
            bucket = buckets.setdefault(label, {"symbol": label, "buy": 0, "sell": 0, "total": 0})
            side = str(order.get("side") or "").upper()
            if side == "BUY":
                bucket["buy"] += 1
            elif side == "SELL":
                bucket["sell"] += 1
            bucket["total"] += 1
        return sorted(buckets.values(), key=lambda item: str(item.get("symbol") or ""))

    @staticmethod
    def _filter_normalized_cancel_orders(orders: list[dict], symbol_code: int, request: Mapping[str, Any]) -> list[dict]:
        side = str(request.get("side") or "").lower()
        order_type = str(request.get("order_type") or "").upper()
        expected_side = {"buy": "BUY", "sell": "SELL"}.get(side)
        out = []
        for order in orders:
            if not isinstance(order, Mapping) or not order.get("is_active"):
                continue
            try:
                if int(order.get("symbol_code")) != int(symbol_code):
                    continue
            except Exception:
                continue
            if expected_side and str(order.get("side") or "").upper() != expected_side:
                continue
            if order_type and order_type not in {"ALL", "BOTH"}:
                normalized_type = str(order.get("order_type") or "").upper()
                if order_type == "LIMIT" and normalized_type != "LIMIT":
                    continue
                if order_type == "MARKET" and normalized_type != "MARKET":
                    continue
            if not AfxAgent._order_id(order):
                continue
            out.append(dict(order))
        return out

    @staticmethod
    def _order_id(order: Mapping[str, Any]) -> Optional[str]:
        oid = order.get("ordId") or order.get("orderId") or order.get("order_id") or order.get("id")
        return str(oid) if oid not in (None, "") else None

    def _cancel_tpsl_orders(self, exchange_client: Any, symbol_code: int, orders: list[dict]) -> tuple[list[Any], list[dict]]:
        cancel_results: list[Any] = []
        cancel_payloads: list[dict] = []
        for order in orders:
            oid = self._order_id(order)
            if not oid:
                continue
            payload = {"symbol_code": symbol_code, "ord_id": oid}
            cancel_payloads.append(payload)
            cancel_results.append(exchange_client.cancel_order(**payload))
        return cancel_results, cancel_payloads

    def _verify_no_matching_tpsl(self, wallet: str, symbol_code: int, reduce_code: int, side_code: Optional[int] = None) -> tuple[bool, list[dict]]:
        remaining: list[dict] = []
        for attempt in range(3):
            if attempt:
                time.sleep(1.0)
            remaining = self._matching_tpsl_orders(wallet, symbol_code, reduce_code, side_code=side_code)
            if not remaining:
                return True, []
        return False, remaining

    def _verify_exactly_one_matching_tpsl(
        self,
        wallet: str,
        symbol_code: int,
        reduce_code: int,
        *,
        side_code: Optional[int] = None,
        trigger_px: Optional[str] = None,
    ) -> tuple[bool, list[dict]]:
        matches: list[dict] = []
        for attempt in range(3):
            if attempt:
                time.sleep(1.0)
            matches = self._matching_tpsl_orders(wallet, symbol_code, reduce_code, side_code=side_code, trigger_px=trigger_px)
            if len(matches) == 1:
                return True, matches
        return False, matches

    def _matching_tpsl_orders(
        self,
        wallet: str,
        symbol_code: int,
        reduce_code: int,
        side_code: Optional[int] = None,
        trigger_px: Optional[str] = None,
    ) -> list[dict]:
        raw = self._get_open_orders(wallet, symbol_code=symbol_code)
        products = self._products_response()
        self._log_safe_order_response(f"tpsl_match symbol_code={symbol_code} reduce_code={reduce_code} side_code={side_code} trigger_px={trigger_px}", raw, products)
        orders = self._orders_from_response(raw)
        if not orders:
            return []
        expected_reduce = {2: "TP_FROM_POSITION", 3: "SL_FROM_POSITION"}.get(reduce_code)
        matches = []
        for order in orders:
            if not isinstance(order, Mapping):
                continue
            normalized = self._normalize_afx_order(order, products)
            if normalized.get("symbol_code") != int(symbol_code):
                continue
            if not normalized.get("is_active"):
                continue
            if side_code is not None and normalized.get("side_code") != side_code:
                continue
            if expected_reduce and normalized.get("reduce_only_option") != expected_reduce:
                continue
            if not normalized.get("trigger_price"):
                continue
            if trigger_px is not None and not self._trigger_prices_equal(normalized.get("trigger_price"), trigger_px):
                continue
            matches.append(normalized)
        return matches

    def _sdk_exchange_client(self, request: Mapping[str, Any]) -> Any:
        wallet, agent_key, _credential_context = self._credential_context(request)
        if not wallet or not agent_key:
            raise RuntimeError("AFX wallet and agent private key are required for order execution")
        try:
            from eth_account import Account
            from afx import AfxClient
            from afx.utils.config import get_environment
        except Exception as exc:
            raise RuntimeError("AFX Python SDK is not installed") from exc

        agent_account = Account.from_key(agent_key)

        class _AgentOnlyWallet:
            def __init__(self, master_address: str, agent: Any) -> None:
                self._master_address = master_address
                self._agent_account = agent

            @property
            def master_address(self) -> str:
                return self._master_address

            @property
            def agent_address(self) -> str:
                return self._agent_account.address

            def sign_with_agent(self, full_message: Any) -> Any:
                return Account.sign_typed_data(
                    private_key=self._agent_account.key,
                    full_message=full_message,
                )

        client = AfxClient(wallet=_AgentOnlyWallet(wallet, agent_account), environment=get_environment(testnet=False))
        return client.exchange

    def _not_implemented(self, request: Mapping[str, Any], message: str) -> dict:
        _wallet, _agent_key, credential_context = self._credential_context(request)
        return _execution_result(request, success=False, error=message, credential_variables=credential_context)

    def _get_json(self, path: str, params: Mapping[str, Any]) -> Any:
        url = f"{self.base_url}{path}?{urllib.parse.urlencode(params)}"
        req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "HermesTradeDesk/1.0"})
        with urllib.request.urlopen(req, timeout=20) as response:  # noqa: S310 - fixed AFX API URL
            return json.loads(response.read().decode("utf-8"))

    @staticmethod
    def _raw_success(raw: Any) -> bool:
        if not isinstance(raw, Mapping):
            return True
        code = raw.get("code")
        if code not in (None, 0, "0"):
            return False
        status = str(raw.get("status") or raw.get("message") or "").lower()
        if status in {"error", "failed", "failure", "rejected"}:
            return False
        return True

    @staticmethod
    def _to_float(value: Any) -> Optional[float]:
        if value in (None, ""):
            return None
        try:
            return float(value)
        except Exception:
            return None

    @staticmethod
    def _fmt(value: Any) -> str:
        if isinstance(value, str):
            return value
        return f"{float(value):.12g}"

    @staticmethod
    def _to_int(value: Any) -> Optional[int]:
        if value in (None, ""):
            return None
        try:
            return int(value)
        except Exception:
            return None

    @staticmethod
    def _apply_price_precision(value: Optional[float], precision: Optional[int]) -> Optional[float]:
        if value is None:
            return None
        if precision is None:
            return value
        factor = 10**precision
        return math.floor(value * factor) / factor

    @staticmethod
    def _product_list(products: Any) -> list[dict]:
        data = products.get("data") if isinstance(products, Mapping) else products
        if isinstance(data, Mapping):
            items = data.get("perpProducts") or data.get("products") or []
        else:
            items = data
        return [dict(item) for item in items] if isinstance(items, list) else []

    @classmethod
    def _product_by_code(cls, products: Any) -> dict[int, dict]:
        out: dict[int, dict] = {}
        for item in cls._product_list(products):
            try:
                out[int(item.get("code"))] = item
            except Exception:
                continue
        return out

    @classmethod
    def _product_for_child(cls, child: Mapping[str, Any], products: Any) -> dict:
        raw_symbol = str(child.get("symbol") or "").upper()
        for item in cls._product_list(products):
            symbol = str(item.get("symbol") or "").upper()
            name = str(item.get("name") or "").upper().replace(" PERP", "")
            base = str(item.get("baseCurrency") or "").upper()
            quote = str(item.get("quoteCurrency") or "").upper()
            settle = str(item.get("settleCurrency") or "").upper()
            aliases = {symbol, name, base}
            for suffix in (quote, settle):
                if suffix:
                    aliases.add(symbol.removesuffix(suffix))
                    aliases.add(name.removesuffix(suffix))
            if raw_symbol in aliases:
                return dict(item)
        if raw_symbol.isdigit():
            for item in cls._product_list(products):
                try:
                    code_raw = item.get("code")
                    if code_raw is not None and int(code_raw) == int(raw_symbol):
                        return dict(item)
                except Exception:
                    continue
        raise ValueError(f"AFX symbol not found in product metadata: {raw_symbol}")

    @classmethod
    def _symbol_code_for_child(cls, child: Mapping[str, Any], products: Any) -> int:
        product = cls._product_for_child(child, products)
        code = product.get("code")
        if code is None:
            raise ValueError(f"AFX product metadata missing code for {child.get('symbol')}")
        return int(code)

    @classmethod
    def _format_price_for_product(cls, value: Any, product: Mapping[str, Any]) -> str:
        return cls._format_decimal_for_product(value, product, value_key="price", rounding=ROUND_HALF_UP)

    @classmethod
    def _format_quantity_for_product(cls, value: Any, product: Mapping[str, Any]) -> str:
        return cls._format_decimal_for_product(value, product, value_key="qty", rounding=ROUND_DOWN)

    @classmethod
    def _format_decimal_for_product(cls, value: Any, product: Mapping[str, Any], *, value_key: str, rounding: str) -> str:
        try:
            amount = Decimal(str(value))
        except (InvalidOperation, ValueError, TypeError) as exc:
            raise ValueError(f"Invalid AFX {value_key}: {value}") from exc

        step_key = "tickSize" if value_key == "price" else "stepSize"
        precision_key = "pricePrecision" if value_key == "price" else "qtyPrecision"
        step_raw = product.get(step_key)
        if step_raw not in (None, "", 0, "0"):
            step = Decimal(str(step_raw))
        else:
            precision = cls._to_int(product.get(precision_key))
            step = Decimal(1).scaleb(-(precision or 0))
        if step <= 0:
            raise ValueError(f"Invalid AFX {step_key}: {step_raw}")
        quantized = (amount / step).to_integral_value(rounding=rounding) * step
        if quantized <= 0:
            raise ValueError(f"AFX {value_key} rounds to zero at step {step}: {value}")
        return cls._format_decimal(quantized)

    @staticmethod
    def _format_decimal(value: Decimal) -> str:
        normalized = value.normalize()
        if normalized == normalized.to_integral_value():
            return format(normalized, "f")
        return format(normalized, "f").rstrip("0").rstrip(".")

    @classmethod
    def _market_slippage_pct_for_product(cls, product: Mapping[str, Any]) -> str:
        available = _combined_casefold_env()
        found = available.get("afx_market_slippage_pct")
        raw_value = found[1] if found else AFX_DEFAULT_MARKET_SLIPPAGE_PCT
        try:
            value = Decimal(str(raw_value))
        except (InvalidOperation, ValueError, TypeError) as exc:
            source = "AFX_MARKET_SLIPPAGE_PCT" if found else "default AFX market slippage"
            raise ValueError(f"Invalid {source}: expected decimal ratio string, got {raw_value!r}") from exc
        if value <= 0:
            raise ValueError("Invalid AFX_MARKET_SLIPPAGE_PCT: must be greater than 0")
        max_raw = product.get("maxSlippagePct")
        if max_raw not in (None, ""):
            try:
                max_value = Decimal(str(max_raw))
            except (InvalidOperation, ValueError, TypeError) as exc:
                raise ValueError(f"Invalid AFX product maxSlippagePct: {max_raw!r}") from exc
            if value > max_value:
                raise ValueError(
                    f"Invalid AFX_MARKET_SLIPPAGE_PCT: {cls._format_decimal(value)} exceeds product maxSlippagePct {cls._format_decimal(max_value)}"
                )
        return cls._format_decimal(value)

    def _child_to_order_payload(self, child: Mapping[str, Any]) -> dict:
        products = self._products_response()
        product = self._product_for_child(child, products)
        code = product.get("code")
        if code is None:
            raise ValueError(f"AFX product metadata missing code for {child.get('symbol')}")
        symbol_code = int(code)
        order_type = str(child.get("order_type") or "").lower()
        side = "BUY" if bool(child.get("is_buy")) else "SELL"
        qty = self._format_quantity_for_product(child.get("size"), product)
        if order_type == "market":
            px = "0"
            ord_type = "MARKET"
            tif = "IOC"
        elif order_type == "limit":
            px = self._format_price_for_product(child.get("price"), product)
            ord_type = "LIMIT"
            tif = "GTC"
        else:
            raise ValueError(f"Unsupported AFX order_type: {order_type}")
        payload = {
            "symbol_code": symbol_code,
            "px": px,
            "qty": qty,
            "side": side,
            "ord_type": ord_type,
            "tif": tif,
        }
        if child.get("reduce_only"):
            payload["reduce_only_option"] = "REDUCE_ONLY"
        if ord_type == "MARKET":
            payload["slippage_pct"] = self._market_slippage_pct_for_product(product)
        return payload

    @classmethod
    def _normalize_positions(
        cls,
        raw: Any,
        products: Any = None,
        *,
        raw_orders: Any = None,
    ) -> list[dict]:
        data = raw.get("data") if isinstance(raw, Mapping) else raw
        if not isinstance(data, list):
            return []
        products_by_code = cls._product_by_code(products)
        # Pre-compute the TP/SL trigger-price map keyed by
        # (symbol_code, closing_side_code). Long positions close with
        # SELL triggers (side=2); short positions close with BUY
        # triggers (side=1). If multiple TPs/SLs exist for the same
        # position, the first matching order in AFX's response order
        # wins.
        tpsl = cls._tpsl_for_symbol_codes(raw_orders)
        out: list[dict] = []
        for item in data:
            if not isinstance(item, Mapping):
                continue
            long_size = cls._to_float(item.get("longSize")) or 0.0
            short_size = cls._to_float(item.get("shortSize")) or 0.0
            raw_code = item.get("symbolCode", item.get("symbol"))
            try:
                symbol_code = int(raw_code)
            except Exception:
                symbol_code = None
            try:
                product = products_by_code.get(symbol_code, {}) if symbol_code is not None else {}
            except Exception:
                product = {}
            symbol = str(product.get("symbol") or item.get("symbolName") or item.get("symbol") or item.get("symbolCode") or "").upper()
            asset = str(product.get("baseCurrency") or symbol.replace("USDC", "") or symbol).upper()
            mark = cls._to_float(item.get("curMarkPx") or item.get("markPx"))
            leverage = cls._to_float(item.get("leverage"))
            margin_mode = str(item.get("positionMarginMode") or item.get("posMarginMode") or "unknown").lower()
            pnl = cls._to_float(item.get("unrealizedPnl"))
            price_precision = cls._to_int(product.get("pricePrecision"))
            if long_size:
                entry_for_symbol = (
                    tpsl.get((symbol_code, 2)) if symbol_code is not None else None
                ) or {}
                entry_value = cls._to_float(item.get("longEntryValue"))
                entry_price = entry_value / abs(long_size) if entry_value is not None and long_size else None
                entry_price = cls._apply_price_precision(entry_price, price_precision)
                out.append({
                    "id": f"{symbol}:long",
                    "symbol": symbol,
                    "asset": asset,
                    "side": "long",
                    "size": abs(long_size),
                    "entry_price": entry_price,
                    "mark_price": mark,
                    "unrealized_pnl": pnl,
                    "roe_pct": None,
                    "liquidation_price": None,
                    "margin_mode": margin_mode,
                    "leverage": leverage,
                    "take_profit": entry_for_symbol.get("take_profit"),
                    "stop_loss": entry_for_symbol.get("stop_loss"),
                    "raw_position": dict(item),
                })
            if short_size:
                entry_for_symbol = (
                    tpsl.get((symbol_code, 1)) if symbol_code is not None else None
                ) or {}
                entry_value = cls._to_float(item.get("shortEntryValue"))
                entry_price = entry_value / abs(short_size) if entry_value is not None and short_size else None
                entry_price = cls._apply_price_precision(entry_price, price_precision)
                out.append({
                    "id": f"{symbol}:short",
                    "symbol": symbol,
                    "asset": asset,
                    "side": "short",
                    "size": abs(short_size),
                    "entry_price": entry_price,
                    "mark_price": mark,
                    "unrealized_pnl": pnl,
                    "roe_pct": None,
                    "liquidation_price": None,
                    "margin_mode": margin_mode,
                    "leverage": leverage,
                    "take_profit": entry_for_symbol.get("take_profit"),
                    "stop_loss": entry_for_symbol.get("stop_loss"),
                    "raw_position": dict(item),
                })
        return out

    @classmethod
    def _tpsl_for_symbol_codes(cls, raw_orders: Any) -> dict[tuple[int, int], dict[str, Any]]:
        """Match AFX TP/SL trigger orders to their position symbol_code.

        Reuses ``_orders_from_response`` to unwrap the production
        envelope shape::

          {
              "code": 0,
              "message": "success",
              "data": {
                  "total": ...,
                  "items": [...]
              }
          }

        .. so the TP/SL enrichment path and the regular
        ``_open_orders`` path interpret the response identically.

        Selection rule (deterministic):
          * Only orders with ``reduce_only_option`` of
            ``TP_FROM_POSITION`` (code 2) or ``SL_FROM_POSITION``
            (code 3) are considered.
          * Only ``is_active=True`` orders are considered
            (status in {"NEW", "OPEN", "ACTIVE", "PENDING",
            "PARTIALLY_FILLED", "ORDER_STATUS_UNTRIGGERED", ""}).
          * Matching is conservative: symbol_code AND closing-side
            must match. Long positions close via SELL orders
            (side=2). Short positions close via BUY orders (side=1).
          * For each (symbol_code, closing_side_code), the first
            matching TP (by raw order iteration order — which
            preserves AFX's API response order) is the canonical
            ``take_profit``. Same for ``stop_loss``.
          * If a symbol has neither TP nor SL, the corresponding
            fields are ``None``.
          * If a symbol has only a TP (or only an SL), the other
            side remains ``None``.

        Returns a dict ``{(symbol_code, side_code):
        {"take_profit": float|None, "stop_loss": float|None}}``.
        A missing entry means "no TP/SL".
        """
        out: dict[tuple[int, int], dict[str, Any]] = {}
        for order in cls._orders_from_response(raw_orders):
            if not isinstance(order, Mapping):
                continue
            # read the raw reduceOnlyOption / status / triggerPx
            # from the unwrapped order.
            reduce_raw = (
                order.get("reduceOnlyOption")
                or order.get("reduce_only_option")
                or order.get("reduceOnly")
            )
            try:
                reduce_code = (
                    int(reduce_raw) if reduce_raw not in (None, "") else 0
                )
            except (TypeError, ValueError):
                reduce_code = 0
            if reduce_code not in (2, 3):  # 2=TP, 3=SL
                continue
            status = str(
                order.get("status") or order.get("orderStatus") or ""
            ).upper()
            if status not in {
                "", "NEW", "OPEN", "ACTIVE", "PENDING",
                "PARTIALLY_FILLED", "ORDER_STATUS_UNTRIGGERED",
            }:
                continue
            try:
                symbol_code = int(
                    order.get("symbol")
                    or order.get("symbolCode")
                    or order.get("symbol_code")
                    or 0
                )
            except (TypeError, ValueError):
                continue
            if symbol_code == 0:
                continue
            try:
                side_code = int(order.get("side") or order.get("sideCode") or order.get("side_code") or 0)
            except (TypeError, ValueError):
                continue
            if side_code not in (1, 2):
                continue
            trigger_price_raw = (
                order.get("triggerPx")
                or order.get("triggerPrice")
                or order.get("trigger_px")
            )
            trigger_price = cls._to_float(trigger_price_raw)
            slot = out.setdefault(
                (symbol_code, side_code), {"take_profit": None, "stop_loss": None}
            )
            if reduce_code == 2 and slot["take_profit"] is None:
                slot["take_profit"] = trigger_price
            elif reduce_code == 3 and slot["stop_loss"] is None:
                slot["stop_loss"] = trigger_price
        return out
