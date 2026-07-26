"""Raydium write-path support for Phase 2A: order placement and cancellation.

This module is intentionally isolated from the frozen read-only code in
raydium_agent.py. It uses lazy imports to break the circular dependency:
raydium_agent imports from us (via the dispatch in execute()), and we
import from raydium_agent lazily inside function bodies.

Public API:
    execute_order(request, accounts_resolver, client_factory, sign_request_fn) -> dict
    execute_cancel(request, accounts_resolver, client_factory, sign_request_fn) -> dict
"""
from __future__ import annotations

import json
import re
import sys
import time
import uuid
from decimal import ROUND_DOWN, Decimal
from typing import Any, Callable, Mapping, Optional

# requests is optional at module level; the actual HTTP call sites
# use it. We declare the import lazily.
try:
    import requests  # type: ignore
except ImportError:
    requests = None  # type: ignore

# Constants duplicated here so the module can be imported in isolation
# (e.g. for testing). They MUST match raydium_agent's values. We also
# re-resolve at call time via _resolve_agent_dependencies() to pick up
# any drift.
RAYDIUM_BASE_URL = "https://api.orderly.org"
RAYDIUM_BROKER_ID = "raydium"
RAYDIUM_NETWORK = "mainnet"
RAYDIUM_REQUEST_TIMEOUT_SECONDS = 15.0


def _resolve_agent_dependencies():
    """Late-bound imports from raydium_agent to break circular imports.

    Returns a tuple of constants and helpers from raydium_agent. Use this
    inside function bodies so we don't depend on raydium_agent being
    fully imported at module load time.
    """
    if 'tradedesk.raydium_agent' in sys.modules:
        ra = sys.modules['tradedesk.raydium_agent']
    else:
        from . import raydium_agent as ra
    return (
        ra.RAYDIUM_BASE_URL,
        ra.RAYDIUM_BROKER_ID,
        ra.RAYDIUM_NETWORK,
        ra.RAYDIUM_REQUEST_TIMEOUT_SECONDS,
        ra.RaydiumAccount,
        ra.RaydiumHttpClient,
        ra._execution_result,
        ra._RaydiumHttpError,
        ra._summarize_payload,
        ra._sign_request,
        ra._format_decimal,
        ra._validate_account_id_field,
        ra._validate_ed25519_key_field,
        ra._resolve_account_credentials,
    )


# -----------------------------------------------------------------------------
# Validation helpers
# -----------------------------------------------------------------------------

_SUPPORTED_ORDER_TYPES = {"LIMIT"}  # Phase 2A: only LIMIT
_SUPPORTED_SIDES = {"BUY", "SELL"}

_CANONICAL_SYMBOL_RE = re.compile(r"^PERP_[A-Z0-9]+_[A-Z0-9]+$")
_CANONICAL_QUOTE = "USDC"

_DEFAULT_TICK_SIZE = Decimal("0.01")
_DEFAULT_STEP_SIZE = Decimal("0.00001")
_MIN_ORDER_QUANTITY = Decimal("0.00001")  # smallest practical above 0
_MIN_NOTIONAL_USDC = Decimal("1")  # Orderly requires min $1 notional


def _normalize_side(side: Any) -> Optional[str]:
    if not isinstance(side, str):
        return None
    s = side.strip().upper()
    return s if s in _SUPPORTED_SIDES else None


def _normalize_order_type(t: Any) -> Optional[str]:
    if not isinstance(t, str):
        return None
    t2 = t.strip().upper()
    return t2 if t2 in _SUPPORTED_ORDER_TYPES else None


def _normalize_symbol(sym: Any) -> Optional[str]:
    if not isinstance(sym, str):
        return None
    s = sym.strip().upper()
    if not s:
        return None
    if _CANONICAL_SYMBOL_RE.match(s):
        return s
    cleaned = s.replace("-", "_").replace("/", "_")
    if cleaned.startswith("PERP_"):
        cleaned = cleaned[5:]
    parts = [part for part in cleaned.split("_") if part]
    if len(parts) == 1:
        base = parts[0]
        quote = _CANONICAL_QUOTE
    elif len(parts) == 2:
        base, quote = parts
    else:
        return None
    if not base or not quote:
        return None
    canonical = f"PERP_{base}_{quote}"
    return canonical if _CANONICAL_SYMBOL_RE.match(canonical) else None


def _normalize_decimal(value: Any) -> Optional[Decimal]:
    if value is None:
        return None
    try:
        d = Decimal(str(value))
    except Exception:
        return None
    if not d.is_finite():
        return None
    if d <= 0:
        return None
    return d


def quantize_to_tick(price: Decimal, tick: Decimal = _DEFAULT_TICK_SIZE) -> Decimal:
    if tick <= 0:
        return price
    return (price / tick).to_integral_value(rounding=ROUND_DOWN) * tick


def quantize_to_step(quantity: Decimal, step: Decimal = _DEFAULT_STEP_SIZE) -> Decimal:
    if step <= 0:
        return quantity
    return (quantity / step).to_integral_value(rounding=ROUND_DOWN) * step


def _validate_account(request: Mapping[str, Any]) -> Optional[str]:
    acc = request.get("account")
    if not isinstance(acc, str) or not acc.strip():
        return None
    return acc.strip()


def _serialize(payload: Mapping[str, Any]) -> str:
    return json.dumps(payload, separators=(",", ":"), sort_keys=False)




# -----------------------------------------------------------------------------
# Per-symbol metadata (fetched from Orderly /v1/public/info/{symbol})
# -----------------------------------------------------------------------------

# Verified hardcoded fallback for PERP_BTC_USDC. The fallback only applies
# when live metadata fetch fails or the caller did not supply pre-fetched
# metadata. Live values are preferred when available.
_FALLBACK_TICK_SIZE = Decimal("0.1")  # verified quote_tick for PERP_BTC_USDC
_FALLBACK_STEP_SIZE = Decimal("0.00001")  # verified base_tick for PERP_BTC_USDC
_FALLBACK_MIN_QUANTITY = Decimal("0.00001")  # verified base_min for PERP_BTC_USDC
_FALLBACK_MIN_NOTIONAL = Decimal("10")  # verified min_notional for PERP_BTC_USDC


def _fetch_symbol_metadata(symbol, *, timeout_seconds=15.0):
    """Fetch per-symbol metadata from GET /v1/public/info/{symbol}.

    Returns the Orderly data dict on success, None on failure.
    No authentication required.
    """
    base_url = "https://api.orderly.org"
    url = f"{base_url}/v1/public/info/{symbol}"
    try:
        response = requests.get(url, timeout=timeout_seconds)
        if not (200 <= response.status_code < 300):
            return None
        body = response.json()
        if not isinstance(body, Mapping):
            return None
        data = body.get("data")
        if isinstance(data, Mapping):
            return dict(data)
        if "base_tick" in body or "quote_tick" in body:
            return dict(body)
        return None
    except Exception:
        return None


def _fetch_symbol_mark_price(symbol, *, timeout_seconds=10.0):
    """Fetch the live mark price for ``symbol`` from a public endpoint.

    Returns a float mark price, or ``None`` on failure. Used by the
    order preflight to validate that a user-supplied price falls within
    Orderly's configured ``price_scope`` band before submitting. Public
    endpoint, no authentication required.
    """
    base_url = "https://api.orderly.org"
    url = f"{base_url}/v1/public/futures/{symbol}"
    try:
        response = requests.get(url, timeout=timeout_seconds)
        if not (200 <= response.status_code < 300):
            return None
        body = response.json()
        if not isinstance(body, Mapping):
            return None
        data = body.get("data")
        if not isinstance(data, Mapping):
            return None
        raw = data.get("mark_price")
        if raw is None:
            return None
        return float(raw)
    except Exception:
        return None


def _preflight_price_scope(
    price: Decimal,
    mark_price: Any,
    price_scope: Any,
) -> Optional[dict]:
    """Return a preflight error dict if ``price`` is outside the
    Orderly price band, otherwise ``None``.

    Orderly's ``price_scope`` is a per-market static fraction (e.g. ``0.6``
    for ETH/BTC) that defines the symmetric band around the current mark
    price. An order price that falls outside ``mark * (1 ± price_scope)``
    is rejected by Orderly with HTTP 400 / code -1103 ("price scope
    requirement"). Surfacing this at the agent level gives the user the
    exact allowed range and the mark price that drives it, instead of a
    raw Orderly error message.

    The preflight is advisory. The agent still submits to Orderly when
    the price is in-band; if Orderly ever tightens the band between
    the preflight and the submit, Orderly's own error remains
    authoritative. This is intentional: the preflight is a UX layer,
    not a policy layer.
    """
    if mark_price is None or price_scope is None:
        return None
    try:
        mark_f = float(mark_price)
        scope_f = float(price_scope)
    except (TypeError, ValueError):
        return None
    if mark_f <= 0 or scope_f <= 0:
        return None
    price_f = float(price)
    lower = mark_f * (1.0 - scope_f)
    upper = mark_f * (1.0 + scope_f)
    if lower <= price_f <= upper:
        return None
    side_hint = "above" if price_f > mark_f else "below"
    return {
        "error": (
            f"Order price {price} is {side_hint} Orderly's ±{scope_f*100:.0f}% "
            f"price band around the current mark price {mark_f:.4f}. "
            f"Allowed range: {lower:.4f} .. {upper:.4f}."
        ),
        "mark_price": mark_f,
        "price_scope": scope_f,
        "lower_bound": lower,
        "upper_bound": upper,
    }


def _resolve_symbol_metadata(request, symbol):
    """Resolve per-symbol metadata for symbol.

    Lookup order:
      1. request['symbol_metadata'] if provided (allows caller pre-fetch)
      2. Live GET /v1/public/info/{symbol}
      3. Verified hardcoded fallback constants for supported Raydium
         contracts that have been live-validated.

    Returns dict with keys:
        tick_size, step_size, min_quantity, min_notional, source, raw
        or None if the symbol cannot be validated.
    """
    supplied = request.get("symbol_metadata") if isinstance(request, Mapping) else None
    if isinstance(supplied, Mapping) and "tick_size" in supplied:
        return {
            "tick_size": Decimal(str(supplied["tick_size"])),
            "step_size": Decimal(str(supplied.get("step_size", supplied.get("base_tick", supplied["tick_size"])))),
            "min_quantity": Decimal(str(supplied.get("min_quantity", supplied.get("base_min", "0.00001")))),
            "min_notional": Decimal(str(supplied.get("min_notional", "10"))),
            "source": "caller_supplied",
            "raw": dict(supplied),
        }

    fetched = _fetch_symbol_metadata(symbol)
    if isinstance(fetched, dict):
        try:
            tick = Decimal(str(fetched["quote_tick"]))
            step = Decimal(str(fetched.get("base_tick", fetched.get("quote_tick"))))
            min_qty = Decimal(str(fetched.get("base_min", "0.00001")))
            min_not = Decimal(str(fetched.get("min_notional", "10")))
            return {
                "tick_size": tick,
                "step_size": step,
                "min_quantity": min_qty,
                "min_notional": min_not,
                "source": "live_fetched",
                "raw": fetched,
            }
        except Exception:
            pass

    if symbol == "PERP_BTC_USDC":
        return {
            "tick_size": _FALLBACK_TICK_SIZE,
            "step_size": _FALLBACK_STEP_SIZE,
            "min_quantity": _FALLBACK_MIN_QUANTITY,
            "min_notional": _FALLBACK_MIN_NOTIONAL,
            "source": "fallback",
            "raw": None,
        }
    return None
def _now_ms() -> int:
    return int(time.time() * 1000)


# -----------------------------------------------------------------------------
# Order placement
# -----------------------------------------------------------------------------

def execute_order(
    request: Mapping[str, Any],
    *,
    accounts_resolver: Callable[[str], list],
    client_factory: Callable,
    sign_request_fn: Callable,
) -> dict:
    """Place one limit order on Raydium (Orderly).

    Input contract (post-TradeDesk routing):
        operation: "order"
        exchange: "raydium"
        account: "example"
        symbol: "PERP_XXX_USDC"
        side: "BUY" | "SELL"
        order_type: "LIMIT"
        price: number/string
        quantity: number/string
        client_order_id: optional string

    Output: normalized result dict with order_id at data.order_id.
    """
    # Late-bind raydium_agent dependencies
    (BASE_URL, BROKER_ID, NETWORK, TIMEOUT_SECONDS, RaydiumAccount, RaydiumHttpClient,
     _execution_result, _RaydiumHttpError, _summarize_payload,
     _sign_request, _format_decimal, _validate_account_id_field,
     _validate_ed25519_key_field, _resolve_account_credentials) = _resolve_agent_dependencies()

    if str(request.get("exchange") or "").lower() != "raydium":
        return _execution_result(
            request, success=False,
            error="Raydium order requires exchange=raydium",
        )

    # Field resolution rule: prefer explicit top-level request fields;
    # fall back to the same field under structured_request (which is
    # where TradeDesk places the original request via _normalize_passthrough).
    # If BOTH are present and DIFFER, reject to avoid a silent override
    # that could submit the wrong live action.
    def _resolve_field(field_name: str):
        sr = request.get("structured_request")
        aliases = [field_name]
        if field_name == "quantity":
            aliases.append("size")
        values = []
        for key in aliases:
            top = request.get(key)
            sr_val = None
            if isinstance(sr, Mapping):
                sr_val = sr.get(key)
            if top is not None:
                values.append((key, "top-level", top))
            if sr_val is not None:
                values.append((key, "structured_request", sr_val))
        if not values:
            return (None, None)
        first_key, first_src, first_val = values[0]
        for key, src, val in values[1:]:
            if str(first_val).strip() != str(val).strip():
                return (
                    None,
                    f"Conflicting values for '{field_name}' via {first_src}:{first_key}={first_val!r} and {src}:{key}={val!r}",
                )
        return (first_val, None)

    _resolved = {}
    _conflicts = []
    for _fld in ("symbol", "side", "order_type", "price", "quantity"):
        _val, _err = _resolve_field(_fld)
        if _err is not None:
            _conflicts.append(_err)
        _resolved[_fld] = _val
    if _conflicts:
        return _execution_result(
            request, success=False,
            error="Ambiguous request fields: " + "; ".join(_conflicts),
        )

    symbol = _normalize_symbol(_resolved["symbol"])
    if symbol is None:
        return _execution_result(
            request, success=False,
            error="Invalid symbol (expected format: PERP_<BASE>_<QUOTE>)",
        )

    side = _normalize_side(_resolved["side"])
    if side is None:
        return _execution_result(
            request, success=False,
            error="Invalid side (expected BUY or SELL)",
        )

    order_type = _normalize_order_type(_resolved["order_type"])
    if order_type is None:
        return _execution_result(
            request, success=False,
            error=f"Unsupported order_type for Raydium (Phase 2A accepts: {sorted(_SUPPORTED_ORDER_TYPES)})",
        )

    price = _normalize_decimal(_resolved["price"])
    if price is None:
        return _execution_result(
            request, success=False,
            error="Invalid price (must be a positive number)",
        )

    quantity = _normalize_decimal(_resolved["quantity"])
    if quantity is None:
        return _execution_result(
            request, success=False,
            error="Invalid quantity (must be a positive number)",
        )

    # Resolve per-symbol metadata (live first; verified fallback only on
    # failure)
    _meta = _resolve_symbol_metadata(request, symbol)
    if _meta is None:
        return _execution_result(
            request, success=False,
            error=f"Unsupported Raydium symbol '{symbol}' (live metadata unavailable or unrecognized)",
        )
    live_tick = _meta["tick_size"]
    live_step = _meta["step_size"]
    live_min_qty = _meta["min_quantity"]
    live_min_notional = _meta["min_notional"]

    if quantity < live_min_qty:
        return _execution_result(
            request, success=False,
            error=f"Quantity {quantity} below Orderly minimum {live_min_qty} (source: {_meta['source']})",
        )

    price_q = quantize_to_tick(price, live_tick)
    quantity_q = quantize_to_step(quantity, live_step)

    if price_q <= 0 or quantity_q <= 0:
        if quantity > 0:
            return _execution_result(
                request, success=False,
                error=f"Quantity {quantity} below Orderly minimum {live_min_qty} after quantization (source: {_meta['source']})",
            )
        return _execution_result(
            request, success=False,
            error=f"After quantization, price={price_q} or quantity={quantity_q} non-positive",
        )

    notional = price_q * quantity_q
    if notional < live_min_notional:
        return _execution_result(
            request, success=False,
            error=f"Order notional {notional} below minimum {live_min_notional} USDC (source: {_meta['source']})",
        )

    # Orderly's price_scope preflight: this is an EXCHANGE constraint, not
    # an agent policy. We surface a clear error before submitting so the
    # user sees the actual allowed range, but Orderly is still
    # authoritative — if it tightens the band between this preflight and
    # the submit, its own error message wins. The preflight is a UX
    # layer, not a policy layer.
    preflight = _preflight_price_scope(
        price_q,
        _fetch_symbol_mark_price(symbol),
        (_meta.get("raw") or {}).get("price_scope"),
    )
    if preflight is not None:
        return _execution_result(
            request,
            success=False,
            error=preflight["error"],
            exchange="raydium",
            exchange_response={
                "preflight": preflight,
                "broker_id": BROKER_ID,
                "network": NETWORK,
            },
        )

    account = _validate_account(request)
    if account is None:
        return _execution_result(
            request, success=False,
            error="Missing or invalid 'account' field",
        )

    creds = _resolve_account_credentials(account)
    if creds is None or not getattr(creds, 'account_id', ''):
        return _execution_result(
            request, success=False,
            error=f"Raydium account '{account}' not resolved (credentials missing or malformed)",
        )

    client_order_id = request.get("client_order_id")
    if not client_order_id or not isinstance(client_order_id, str) or not client_order_id.strip():
        client_order_id = f"raydium-{uuid.uuid4().hex[:16]}"

    payload = {
        "symbol": symbol,
        "client_order_id": client_order_id,
        "side": side,
        "order_type": order_type,
        "order_price": _format_decimal(price_q),
        "order_quantity": _format_decimal(quantity_q),
    }

    try:
        body_str = _serialize(payload)
        timestamp_ms = _now_ms()
        signature = sign_request_fn(
            private_key_bytes=creds.private_key_bytes,
            public_key_b58=creds.public_key_b58,
            timestamp_ms=timestamp_ms,
            method="POST",
            url_path="/v1/order",
            url_search="",
            body=body_str,
        )
        headers = {
            "Content-Type": "application/json",
            "orderly-timestamp": str(timestamp_ms),
            "orderly-account-id": creds.account_id,
            "orderly-key": f"ed25519:{creds.public_key_b58}",
            "orderly-signature": signature,
        }
        try:
            response = requests.post(
                f"{BASE_URL}/v1/order",
                headers=headers,
                data=body_str,
                timeout=TIMEOUT_SECONDS,
            )
        except requests.RequestException as exc:
            return _execution_result(
                request, success=False,
                error=f"HTTP transport error: {exc}",
                exchange_response={"status": 0, "transport": str(exc)},
            )
        try:
            resp_payload = response.json()
        except Exception:
            resp_payload = {"raw_text": response.text}

        if not (200 <= response.status_code < 300):
            return _execution_result(
                request, success=False,
                error=f"Orderly HTTP {response.status_code}: {_summarize_payload(resp_payload)}",
                exchange_response={"status": response.status_code, "payload": resp_payload},
            )

        order_id = None
        data = resp_payload.get("data") if isinstance(resp_payload, Mapping) else None
        if isinstance(data, Mapping):
            order_id = data.get("order_id") or data.get("orderId")

        return _execution_result(
            request,
            success=True,
            exchange_response={
                "raw": resp_payload,
                "broker_id": BROKER_ID,
                "network": NETWORK,
            },
            order_id=order_id,
            client_order_id=client_order_id,
            symbol=symbol,
            side=side,
            order_type=order_type,
            price=_format_decimal(price_q),
            quantity=_format_decimal(quantity_q),
            metadata_source=_meta["source"],
            metadata_tick_size=_format_decimal(live_tick),
            metadata_step_size=_format_decimal(live_step),
        )
    except _RaydiumHttpError as exc:
        return _execution_result(
            request,
            success=False,
            error=exc.message,
            exchange_response={"status": exc.status_code, "payload": exc.payload},
        )


# -----------------------------------------------------------------------------
# Batch (ladder) order placement
# -----------------------------------------------------------------------------

def execute_batch_orders(
    request: Mapping[str, Any],
    *,
    accounts_resolver: Callable[[str], list],
    client_factory: Callable,
    sign_request_fn: Callable,
) -> dict:
    """Place a ladder of limit orders on Raydium (Orderly).

    Submits each child order sequentially via Orderly's POST /v1/order.
    Orderly does not expose a true batch-insert endpoint; the historical
    batch-orders path was a client-side concept that mapped to N
    individual POSTs. This helper implements that contract: per-child
    success/failure is reported in ``child_results``; a single child
    failure does NOT roll back already-placed children. The helper is
    intentionally conservative — we surface partial-success honestly
    rather than masking the per-child outcome.

    Input contract (post-TradeDesk routing):
        operation: "batch_orders"
        exchange: "raydium"
        account: "example"
        child_orders: list of dicts, each shaped like a single-order
            StructuredTradeRequest (version, operation="order",
            exchange, account, symbol, side, order_type, price, quantity,
            client_order_id)
        parent_operation: "ladder" (informational; for the result envelope)

    Output envelope:
        success:        True only if EVERY child succeeded
        partial:        True if SOME children succeeded
        child_results:  list of per-child envelopes, in input order
        attempt_count:  number of child orders attempted
        accepted_count: number of child orders accepted by Orderly
        failed_count:   number of child orders that failed
    """
    # Late-bind raydium_agent dependencies, same pattern as execute_order.
    (BASE_URL, BROKER_ID, NETWORK, TIMEOUT_SECONDS, RaydiumAccount, RaydiumHttpClient,
     _execution_result, _RaydiumHttpError, _summarize_payload,
     _sign_request, _format_decimal, _validate_account_id_field,
     _validate_ed25519_key_field, _resolve_account_credentials) = _resolve_agent_dependencies()

    if str(request.get("exchange") or "").lower() != "raydium":
        return _execution_result(
            request, success=False,
            error="Raydium batch_orders requires exchange=raydium",
        )

    # Snapshot the child list so we never mutate the caller's input. The
    # helper must not alter request["child_orders"] in place; downstream
    # callers (the TradeDesk renderer, the wizard) read it back after the
    # request returns.
    child_orders = list(request.get("child_orders") or [])
    if not child_orders:
        return _execution_result(
            request, success=False,
            error="Raydium batch_orders requires a non-empty child_orders list",
        )

    # Resolve credentials ONCE up front; the per-child execute_order()
    # path re-resolves, but resolving up front surfaces a misconfigured
    # account BEFORE we place any child.
    account = str(request.get("account") or "").strip()
    if not account:
        return _execution_result(
            request, success=False,
            error="Missing or invalid 'account' field",
        )
    creds = _resolve_account_credentials(account)
    if creds is None or not getattr(creds, "account_id", ""):
        return _execution_result(
            request, success=False,
            error=f"Raydium account '{account}' not resolved (credentials missing or malformed)",
        )

    # Account resolver and client_factory hooks: kept for parity with
    # execute_order but unused here (we already have the credentials
    # object; execute_order() constructs its own client per child).
    _ = accounts_resolver
    _ = client_factory

    child_results: list[dict] = []
    accepted = 0
    for index, child in enumerate(child_orders, start=1):
        # The per-child StructuredTradeRequest. Do NOT mutate child
        # itself; pass a shallow copy so execute_order()'s internal
        # _safe_request_snapshot and structured_request enrichment
        # don't bleed back into the caller's dict.
        if not isinstance(child, Mapping):
            child_results.append({
                "child_id": index,
                "success": False,
                "error": "child entry is not a mapping",
            })
            continue
        child_request = dict(child)
        # Force the operation to "order" so execute_order() does the
        # right thing. TradeDesk already sets it, but be defensive.
        child_request["operation"] = "order"
        # Inject the resolved account so per-child validation passes
        # even if the wizard left it blank.
        child_request.setdefault("account", account)
        child_request.setdefault("exchange", "raydium")

        result = execute_order(
            child_request,
            accounts_resolver=accounts_resolver,
            client_factory=client_factory,
            sign_request_fn=sign_request_fn,
        )
        result = dict(result) if isinstance(result, Mapping) else {}
        # Stamp the per-child id and the input index for caller-side
        # reporting. We do NOT mutate the per-child result; we copy.
        result["child_id"] = index
        child_results.append(result)
        if result.get("success"):
            accepted += 1

    attempt_count = len(child_results)
    # Honest accounting: success only if every child succeeded.
    success = accepted == attempt_count
    partial = 0 < accepted < attempt_count
    return _execution_result(
        request,
        success=success,
        child_results=child_results,
        attempt_count=attempt_count,
        accepted_count=accepted,
        failed_count=attempt_count - accepted,
        partial=partial,
        # Convention: the ladder renderer (TradeDesk._format_ladder_*)
        # reads ``child_results`` and falls back to ``children`` /
        # ``structured_request.child_orders``. Populate all three so
        # the existing renderer contract is satisfied.
        children=child_results,
        structured_request=dict(request),
    )


# -----------------------------------------------------------------------------
# Position-manager: Take-profit (TP) and stop-loss (SL) standalone orders
def execute_set_tpsl(
    request: Mapping[str, Any],
    *,
    accounts_resolver: Callable[[str], list],
    client_factory: Callable,
    sign_request_fn: Callable,
    fetch_active_tpsl: Optional[Callable] = None,
    cancel_algo_order: Optional[Callable] = None,
) -> dict:
    """Place or remove a take-profit (TP) or stop-loss (SL) on a Raydium (Orderly) position.

    Orderly exposes its position-manager algo orders at
    ``POST /v1/algo/order``. The endpoint accepts a single algo
    request per call; for position-bound TP/SL we use ``algo_type:
    POSITIONAL_TP_SL`` and pass the TP (or SL) as a single
    ``child_order`` with ``type: CLOSE_POSITION`` so Orderly closes
    the existing position at the trigger price. This is the
    documented mechanism for the Position Manager "Set TP / Set SL"
    buttons in the trade menu wizard (Orderly enforces a max of
    one untriggered POSITIONAL_TP_SL per symbol per side).

    For removal (``price == 0``), we list active POSITIONAL_TP_SL
    algo orders on the same symbol+side and DELETE them via
    ``DELETE /v1/algo/order?order_id=X&symbol=Y``. The remove
    flow uses the ``fetch_active_tpsl`` and ``cancel_algo_order``
    callbacks. If these are not provided, ``price == 0`` returns
    a clear error (back-compat: callers can opt out of the remove
    flow by not providing the callbacks).

    Input contract (post-TradeDesk routing):
        operation: "set_tp" | "set_sl"
        exchange:  "raydium"
        account:   "<account_name>"
        symbol:    "PERP_XXX_USDC"     (top-level OR under structured_request
                                         OR under ``position`` dict)
        side:      "buy" | "sell" | "long" | "short"
                                         (position-side aliases ``"long"`` /
                                         ``"short"`` from the Raydium positions
                                         endpoint are accepted and translated
                                         to ``"BUY"`` / ``"SELL"``. The close
                                         side sent to Orderly is OPPOSITE of
                                         position side: a long position's TP
                                         closes with SELL, a short's with BUY.)
        price:     <trigger_price>    (0 = remove existing TP/SL; uses
                                         the ``fetch_active_tpsl`` and
                                         ``cancel_algo_order`` callbacks)
        position:  Optional[dict]      (carries the wizard's selected
                                         position; ``side`` and ``symbol``
                                         are extracted from here as a
                                         fallback when top-level is empty)

    Output envelope:
        success:            True if the algo-order POST returned 2xx
                            with no error payload.
        verification_status: "confirmed_resting" when the POST succeeded
                            and Orderly accepted the algo order. TradeDesk
                            renderer reads this field
                            (tradedesk/tradedesk.py:582).
        order_id:           Echoed Orderly algo order id (when present in
                            the response payload).
        trigger_price:      The price that was POSTed (string).
        is_tp:              True for set_tp, False for set_sl.
        account:            Echoed back from the request.
        exchange_response:  The raw Orderly response payload.

    Wire format (from Orderly OpenAPI spec at /v1/algo/order;
    validated against the live API in /trade):
        {
          "symbol":               "PERP_BTC_USDC",
          "algo_type":            "POSITIONAL_TP_SL",
          "trigger_price_type":   "MARK_PRICE",
          "child_orders": [
            {
              "symbol":             "PERP_BTC_USDC",
              "algo_type":          "TAKE_PROFIT" | "STOP_LOSS",
              "side":               "BUY" | "SELL"   (opposite of position)
              "type":               "CLOSE_POSITION",
              "trigger_price_type": "MARK_PRICE",
              "trigger_price":      <decimal>,
              "reduce_only":        true
            }
          ]
        }
    """
    # Late-bind dependencies, same pattern as execute_order / batch.
    (BASE_URL, BROKER_ID, NETWORK, TIMEOUT_SECONDS, RaydiumAccount, RaydiumHttpClient,
     _execution_result, _RaydiumHttpError, _summarize_payload,
     _sign_request, _format_decimal, _validate_account_id_field,
     _validate_ed25519_key_field, _resolve_account_credentials) = _resolve_agent_dependencies()

    if str(request.get("exchange") or "").lower() != "raydium":
        return _execution_result(
            request, success=False,
            error="Raydium set_tp/set_sl requires exchange=raydium",
        )

    operation = str(request.get("operation") or "")
    is_tp = operation == "set_tp"
    is_sl = operation == "set_sl"
    if not is_tp and not is_sl:
        return _execution_result(
            request, success=False,
            error=f"Raydium set_tpsl requires operation='set_tp' or 'set_sl' (got {operation!r})",
        )

    # Field resolution: prefer top-level request fields; fall back to
    # the same field under structured_request (TradeDesk passthrough
    # nests the original request). If ``position`` is provided, prefer
    # its ``symbol`` and ``side`` over the request's top-level values
    # — the wizard passes the selected position dict and that's the
    # source of truth for which side / symbol the user picked.
    sr = request.get("structured_request") if isinstance(request, Mapping) else None
    if not isinstance(sr, Mapping):
        sr = {}

    position = request.get("position") if isinstance(request, Mapping) else None
    if not isinstance(position, Mapping):
        position = {}

    def _coalesce(field_name: str) -> Any:
        for src in (request, sr, position):
            if isinstance(src, Mapping):
                v = src.get(field_name)
                if v is not None and str(v) != "":
                    return v
        return None

    symbol_raw = _coalesce("symbol")
    if not symbol_raw:
        return _execution_result(
            request, success=False,
            error="Missing 'symbol' field for Raydium set_tp/set_sl",
        )
    symbol = _normalize_symbol(str(symbol_raw))
    if symbol is None:
        return _execution_result(
            request, success=False,
            error="Invalid symbol (expected format: PERP_<BASE>_<QUOTE>)",
        )

    side_raw = _coalesce("side")
    if not side_raw:
        return _execution_result(
            request, success=False,
            error="Missing 'side' field for Raydium set_tp/set_sl",
        )
    side_norm = _normalize_side(str(side_raw))
    # The wizard passes the selected position dict (which carries
    # Raydium's canonical position-side ``"long"`` / ``"short"``).
    # ``_normalize_side`` only accepts ``"BUY"`` / ``"SELL"``, so the
    # position-side aliases need to be translated here. We keep the
    # upstream helper unchanged to avoid touching the broader order
    # flow; this helper is the only place that needs the alias.
    if side_norm is None:
        sl = str(side_raw).strip().lower()
        if sl == "long":
            side_norm = "BUY"
        elif sl == "short":
            side_norm = "SELL"
    if side_norm is None:
        return _execution_result(
            request, success=False,
            error=(
                f"Invalid side {side_raw!r} (expected buy, sell, long, or short)"
            ),
        )
    position_side = side_norm  # BUY = long, SELL = short

    price_raw = _coalesce("price")
    if price_raw is None:
        return _execution_result(
            request, success=False,
            error="Missing 'price' field for Raydium set_tp/set_sl",
        )
    try:
        trigger_price = Decimal(str(price_raw))
    except Exception:
        return _execution_result(
            request, success=False,
            error=f"Invalid trigger price: {price_raw!r}",
        )
    if trigger_price < 0:
        return _execution_result(
            request, success=False,
            error=f"Invalid trigger price {price_raw!r} (must be non-negative)",
        )
    if trigger_price == 0:
        # Remove flow: list active POSITIONAL_TP_SL algo orders on
        # the same symbol+side and cancel them. Orderly's cancel
        # endpoint is DELETE /v1/algo/order?order_id=X&symbol=Y.
        # We return success=True with cancelled_order_ids so the
        # wizard can confirm the removal. If the position side and
        # algo side don't match (e.g. a stale algo from a previous
        # position), the algo is still canceled.
        if fetch_active_tpsl is None or cancel_algo_order is None:
            return _execution_result(
                request, success=False,
                error=(
                    "Cannot remove TP/SL: cancel_algo_order / "
                    "fetch_active_tpsl callbacks were not provided "
                    "to execute_set_tpsl."
                ),
            )
        # Resolve credentials first.
        account_for_remove = str(request.get("account") or "").strip()
        if not account_for_remove:
            return _execution_result(
                request, success=False,
                error="Missing or invalid 'account' field for TP/SL removal",
            )
        creds_for_remove = _resolve_account_credentials(account_for_remove)
        if creds_for_remove is None or not getattr(
            creds_for_remove, "account_id", ""
        ):
            return _execution_result(
                request, success=False,
                error=(
                    f"Raydium account '{account_for_remove}' not resolved "
                    "(credentials missing or malformed) for TP/SL removal"
                ),
            )
        # Expected algo side: opposite of position side.
        expected_algo_side = "SELL" if position_side == "BUY" else "BUY"
        try:
            algo_orders = fetch_active_tpsl(creds_for_remove)
        except Exception as exc:
            return _execution_result(
                request, success=False,
                error=f"Failed to list active algo orders for removal: {exc}",
            )
        # Find the matching algo order (one per symbol+side per Orderly).
        target = None
        for ao in algo_orders or []:
            if (
                ao.get("symbol") == symbol
                and ao.get("side") == expected_algo_side
            ):
                target = ao
                break
        if target is None:
            # No active algo order to remove. Idempotent success.
            return _execution_result(
                request,
                success=True,
                verification_status="confirmed_resting",
                is_remove=True,
                cancelled_order_ids=[],
                removed=False,
                account=account_for_remove,
                exchange="raydium",
                structured_request=dict(request),
                exchange_response={
                    "removed": False,
                    "reason": "no active POSITIONAL_TP_SL algo order found",
                },
            )
        algo_order_id = target.get("algo_order_id")
        if algo_order_id is None:
            return _execution_result(
                request, success=False,
                error="Active algo order has no algo_order_id; cannot cancel",
            )
        # Cancel the algo order. If Orderly returns "already complete"
        # / "already cancelled" / "not found" (race condition: another
        # path cancelled the algo between our fetch and our DELETE),
        # treat as idempotent success because the user's intent —
        # "no active algo on this position" — is already satisfied.
        try:
            cancel_response = cancel_algo_order(
                creds_for_remove, algo_order_id, symbol,
            )
        except _RaydiumHttpError as exc:
            err_msg = str(exc)
            err_msg_lower = err_msg.lower()
            if (
                "already complete" in err_msg_lower
                or "already cancelled" in err_msg_lower
                or "already canceled" in err_msg_lower
                or "not found" in err_msg_lower
            ):
                note = (
                    f"algo order {algo_order_id} was already {err_msg[:120]}"
                )
                return _execution_result(
                    request,
                    success=True,
                    verification_status="confirmed_resting",
                    is_remove=True,
                    cancelled_order_ids=[algo_order_id],
                    removed=True,
                    idempotent=True,
                    note=note,
                    account=account_for_remove,
                    exchange="raydium",
                    structured_request=dict(request),
                    exchange_response={
                        "removed": True,
                        "idempotent": True,
                        "note": note,
                        "algo_order_id": algo_order_id,
                        "symbol": symbol,
                        "side": expected_algo_side,
                        "error": err_msg,
                    },
                )
            return _execution_result(
                request, success=False,
                error=f"Failed to cancel algo order {algo_order_id}: {exc}",
                exchange_response={
                    "status": getattr(exc, "status_code", None),
                    "payload": getattr(exc, "payload", None),
                },
            )
        except Exception as exc:
            return _execution_result(
                request, success=False,
                error=f"Failed to cancel algo order {algo_order_id}: {exc}",
            )
        return _execution_result(
            request,
            success=True,
            verification_status="confirmed_resting",
            is_remove=True,
            cancelled_order_ids=[algo_order_id],
            removed=True,
            account=account_for_remove,
            exchange="raydium",
            structured_request=dict(request),
            exchange_response={
                "removed": True,
                "algo_order_id": algo_order_id,
                "symbol": symbol,
                "side": expected_algo_side,
                "cancel_response": cancel_response,
            },
        )

    account = str(request.get("account") or "").strip()
    if not account:
        return _execution_result(
            request, success=False,
            error="Missing or invalid 'account' field",
        )
    creds = _resolve_account_credentials(account)
    if creds is None or not getattr(creds, "account_id", ""):
        return _execution_result(
            request, success=False,
            error=f"Raydium account '{account}' not resolved (credentials missing or malformed)",
        )

    # Close-side: TP/SL closes the position, so the close side is the
    # opposite of the position side. A long position (BUY) closes with
    # SELL; a short position (SELL) closes with BUY.
    close_side = "SELL" if position_side == "BUY" else "BUY"

    # Merge-and-replace: Orderly enforces 'Maximum 1 untriggered
    # POSITIONAL_TP_SL order per user per symbol per side'. To set
    # BOTH TP and SL on the same position, we must send a single
    # POSITIONAL_TP_SL with multiple child_orders. If an active
    # POSITIONAL_TP_SL already exists on (symbol, close_side), we
    # fetch its children, merge them with the new leg the user just
    # provided, DELETE the existing algo, then POST the merged payload.
    existing_children: list[dict] = []
    previous_algo_order_id = None
    if fetch_active_tpsl is not None:
        try:
            existing = fetch_active_tpsl(creds)
        except Exception:
            existing = []
        for ao in existing or []:
            if (
                ao.get("symbol") == symbol
                and ao.get("side") == close_side
            ):
                previous_algo_order_id = ao.get("algo_order_id")
                for child in ao.get("child_orders") or []:
                    if (
                        isinstance(child, Mapping)
                        and child.get("algo_type") in ("TAKE_PROFIT", "STOP_LOSS")
                    ):
                        # Orderly may return a TAKE_PROFIT child with
                        # no trigger_price (the user only set SL).
                        # Skip children without a trigger_price; the
                        # merged payload only includes priced children.
                        tp = child.get("trigger_price")
                        if tp is None:
                            continue
                        try:
                            tp_str = _format_decimal(float(tp))
                        except (TypeError, ValueError):
                            continue
                        existing_children.append({
                            "symbol": symbol,
                            "algo_type": child["algo_type"],
                            "side": child.get("side", close_side),
                            "type": "CLOSE_POSITION",
                            "trigger_price_type": "MARK_PRICE",
                            "trigger_price": tp_str,
                            "reduce_only": True,
                        })
                break  # one POSITIONAL_TP_SL per (symbol, side)

    # Upsert the user's new leg into the children list. Drop any
    # existing child of the same leg type (replace), then append.
    new_leg_algo_type = "TAKE_PROFIT" if is_tp else "STOP_LOSS"
    new_child = {
        "symbol": symbol,
        "algo_type": new_leg_algo_type,
        "side": close_side,
        "type": "CLOSE_POSITION",
        "trigger_price_type": "MARK_PRICE",
        "trigger_price": _format_decimal(float(trigger_price)),
        "reduce_only": True,
    }
    merged_children = [
        c for c in existing_children
        if c.get("algo_type") != new_leg_algo_type
    ]
    merged_children.append(new_child)

    # DELETE the previous algo BEFORE posting the new one (atomic-ish
    # replace). If the DELETE fails because the algo is already gone
    # (race condition: another path cancelled it), proceed to POST.
    if (
        previous_algo_order_id is not None
        and cancel_algo_order is not None
    ):
        try:
            cancel_algo_order(creds, previous_algo_order_id, symbol)
        except _RaydiumHttpError as exc:
            err_msg = (str(exc) or "").lower()
            if not (
                "already complete" in err_msg
                or "already cancelled" in err_msg
                or "already canceled" in err_msg
                or "not found" in err_msg
            ):
                return _execution_result(
                    request, success=False,
                    error=(
                        f"Failed to replace previous algo "
                        f"{previous_algo_order_id}: {exc}"
                    ),
                    exchange_response={
                        "status": getattr(exc, "status_code", None),
                        "payload": getattr(exc, "payload", None),
                    },
                )

    # Build the Orderly POSITIONAL_TP_SL algo order payload. From the
    # Orderly OpenAPI spec at /v1/algo/order, the wire format is:
    payload = {
        "symbol": symbol,
        "algo_type": "POSITIONAL_TP_SL",
        "trigger_price_type": "MARK_PRICE",
        "child_orders": [
            {
                # The user's new leg is the LAST entry so the merge
                # above (drop existing same-type + append) places it
                # correctly. merged_children already contains all
                # children in the correct order.
                **c,
            } for c in merged_children
        ],
    }

    # Sign and POST. Use the same `requests.post` + `sign_request_fn`
    # pattern as execute_order. The 8-byte ed25519 signature is required
    # for any private endpoint.
    body_str = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    timestamp_ms = _now_ms()
    try:
        signature = sign_request_fn(
            private_key_bytes=creds.private_key_bytes,
            public_key_b58=creds.public_key_b58,
            timestamp_ms=timestamp_ms,
            method="POST",
            url_path="/v1/algo/order",
            url_search="",
            body=body_str,
        )
    except Exception as exc:
        return _execution_result(
            request, success=False,
            error=f"Raydium sign_request failed: {exc}",
            exchange="raydium",
        )

    headers = {
        "Content-Type": "application/json",
        "orderly-timestamp": str(timestamp_ms),
        "orderly-account-id": creds.account_id,
        "orderly-key": f"ed25519:{creds.public_key_b58}",
        "orderly-signature": signature,
    }

    try:
        response = requests.post(
            f"{BASE_URL}/v1/algo/order",
            headers=headers,
            data=body_str,
            timeout=TIMEOUT_SECONDS,
        )
    except requests.RequestException as exc:
        return _execution_result(
            request, success=False,
            error=f"HTTP transport error: {exc}",
            exchange_response={"status": 0, "transport": str(exc)},
        )

    raw_status = response.status_code
    try:
        raw_body = response.json()
    except Exception:
        raw_body = {"success": False, "message": response.text or ""}

    success = (
        200 <= raw_status < 300
        and isinstance(raw_body, Mapping)
        and raw_body.get("success", True) is not False
    )
    if not success:
        err_msg = (
            (raw_body.get("message") if isinstance(raw_body, Mapping) else None)
            or (raw_body.get("error") if isinstance(raw_body, Mapping) else None)
            or "unknown error"
        )
        return _execution_result(
            request, success=False,
            error=f"Orderly HTTP {raw_status}: {err_msg}",
            exchange_response={
                "status": raw_status,
                "payload": raw_body,
                "request": payload,
            },
        )

    # Surface the algo-order id when Orderly returns one. The response
    # shape for /v1/algo/order is data.order_id (an int) — see the
    # Orderly OpenAPI schema for create-algo-order.
    order_id = None
    if isinstance(raw_body, Mapping):
        data = raw_body.get("data")
        if isinstance(data, Mapping):
            order_id = data.get("order_id") or data.get("orderId")
            if order_id is None and isinstance(data.get("order"), Mapping):
                order_id = (
                    data["order"].get("order_id")
                    or data["order"].get("orderId")
                )

    return _execution_result(
        request,
        success=True,
        verification_status="confirmed_resting",
        order_id=order_id,
        trigger_price=_format_decimal(trigger_price),
        is_tp=is_tp,
        previous_algo_order_id=previous_algo_order_id,
        merged_child_orders=merged_children,
        account=account,
        exchange="raydium",
        structured_request=dict(request),
        exchange_response={
            "status": raw_status,
            "payload": raw_body,
            "request": payload,
            "previous_algo_order_id": previous_algo_order_id,
        },
    )


# -----------------------------------------------------------------------------
# Order cancellation
# -----------------------------------------------------------------------------

def execute_cancel(
    request: Mapping[str, Any],
    *,
    accounts_resolver: Callable[[str], list],
    client_factory: Callable,
    sign_request_fn: Callable,
) -> dict:
    """Cancel exactly one Raydium order by order_id.

    Input contract:
        operation: "cancel_order"
        exchange: "raydium"
        account: "example"
        symbol: "PERP_XXX_USDC"
        order_id: int/string

    Output: normalized result dict with order_id preserved.
    """
    # Late-bind raydium_agent dependencies
    (BASE_URL, BROKER_ID, NETWORK, TIMEOUT_SECONDS, RaydiumAccount, RaydiumHttpClient,
     _execution_result, _RaydiumHttpError, _summarize_payload,
     _sign_request, _format_decimal, _validate_account_id_field,
     _validate_ed25519_key_field, _resolve_account_credentials) = _resolve_agent_dependencies()

    if str(request.get("exchange") or "").lower() != "raydium":
        return _execution_result(
            request, success=False,
            error="Raydium cancel requires exchange=raydium",
        )

    # Field resolution rule: prefer top-level, fall back to structured_request,
    # reject if both present and differ (ambiguity guard).
    def _resolve_field(field_name: str):
        sr = request.get("structured_request")
        aliases = [field_name]
        if field_name == "quantity":
            aliases.append("size")
        values = []
        for key in aliases:
            top = request.get(key)
            sr_val = None
            if isinstance(sr, Mapping):
                sr_val = sr.get(key)
            if top is not None:
                values.append((key, "top-level", top))
            if sr_val is not None:
                values.append((key, "structured_request", sr_val))
        if not values:
            return (None, None)
        first_key, first_src, first_val = values[0]
        for key, src, val in values[1:]:
            if str(first_val).strip() != str(val).strip():
                return (
                    None,
                    f"Conflicting values for '{field_name}' via {first_src}:{first_key}={first_val!r} and {src}:{key}={val!r}",
                )
        return (first_val, None)

    _cancel_conflicts = []
    _sym_val, _err = _resolve_field("symbol")
    if _err: _cancel_conflicts.append(_err)
    _oid_val, _err = _resolve_field("order_id")
    if _err: _cancel_conflicts.append(_err)
    if _cancel_conflicts:
        return _execution_result(
            request, success=False,
            error="Ambiguous request fields: " + "; ".join(_cancel_conflicts),
        )

    symbol = _normalize_symbol(_sym_val)
    if symbol is None:
        return _execution_result(
            request, success=False,
            error="Invalid symbol (expected format: PERP_<BASE>_<QUOTE>)",
        )

    raw_oid = _oid_val
    if raw_oid is None or (isinstance(raw_oid, str) and not raw_oid.strip()):
        return _execution_result(
            request, success=False,
            error="Missing or invalid 'order_id' field",
        )
    try:
        order_id = int(raw_oid)
    except (ValueError, TypeError):
        return _execution_result(
            request, success=False,
            error=f"Invalid order_id {raw_oid!r} (must be integer or numeric string)",
        )
    if order_id <= 0:
        return _execution_result(
            request, success=False,
            error=f"Invalid order_id {order_id} (must be positive)",
        )

    account = _validate_account(request)
    if account is None:
        return _execution_result(
            request, success=False,
            error="Missing or invalid 'account' field",
        )

    creds = _resolve_account_credentials(account)
    if creds is None or not getattr(creds, 'account_id', ''):
        return _execution_result(
            request, success=False,
            error=f"Raydium account '{account}' not resolved (credentials missing or malformed)",
        )

    from urllib.parse import urlencode
    search = "?" + urlencode([("order_id", order_id), ("symbol", symbol)])

    timestamp_ms = _now_ms()
    signature = sign_request_fn(
        private_key_bytes=creds.private_key_bytes,
        public_key_b58=creds.public_key_b58,
        timestamp_ms=timestamp_ms,
        method="DELETE",
        url_path="/v1/order",
        url_search=search,
        body="",
    )
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "orderly-timestamp": str(timestamp_ms),
        "orderly-account-id": creds.account_id,
        "orderly-key": f"ed25519:{creds.public_key_b58}",
        "orderly-signature": signature,
    }

    try:
        response = requests.delete(
            f"{BASE_URL}/v1/order{search}",
            headers=headers,
            timeout=TIMEOUT_SECONDS,
        )
    except requests.RequestException as exc:
        return _execution_result(
            request, success=False,
            error=f"HTTP transport error: {exc}",
            exchange_response={"status": 0, "transport": str(exc)},
        )

    try:
        resp_payload = response.json()
    except Exception:
        resp_payload = {"raw_text": response.text}

    if not (200 <= response.status_code < 300):
        return _execution_result(
            request, success=False,
            error=f"Orderly HTTP {response.status_code}: {_summarize_payload(resp_payload)}",
            exchange_response={"status": response.status_code, "payload": resp_payload},
        )

    return _execution_result(
        request,
        success=True,
        exchange_response={
            "raw": resp_payload,
            "broker_id": BROKER_ID,
            "network": NETWORK,
        },
        order_id=order_id,
        symbol=symbol,
    )


def execute_cancel_group(
    request: Mapping[str, Any],
    *,
    accounts_resolver: Callable[[str], list],
    client_factory: Callable,
    sign_request_fn: Callable,
) -> dict:
    """Raydium grouped cancellation by exact canonical symbol + side.

    This path is intentionally narrow:
      1) fresh open-orders read
      2) exact symbol + exact side filter
      3) cancel each exact order_id via execute_cancel
      4) post-read verification
    """
    (BASE_URL, BROKER_ID, NETWORK, TIMEOUT_SECONDS, RaydiumAccount, RaydiumHttpClient,
     _execution_result, _RaydiumHttpError, _summarize_payload,
     _sign_request, _format_decimal, _validate_account_id_field,
     _validate_ed25519_key_field, _resolve_account_credentials) = _resolve_agent_dependencies()

    def _safe_request_snapshot(src: Mapping[str, Any]) -> dict:
        return dict(src) if isinstance(src, Mapping) else {}

    req = _safe_request_snapshot(request)
    if str(req.get("exchange") or "").lower() != "raydium":
        return _execution_result(request, success=False, error="Raydium cancel requires exchange=raydium")

    account = _validate_account(request)
    if account is None:
        return _execution_result(request, success=False, error="Missing or invalid 'account' field")

    # TradeDesk._normalize_passthrough nests the original request under
    # ``structured_request``. The grouped-cancel helper must therefore
    # resolve ``symbol`` / ``side`` from both top-level AND the nested
    # ``structured_request`` (mirroring the ``_resolve_field`` pattern
    # used by the single-order ``execute_cancel`` path). Reading only the
    # top-level key would yield ``None`` and produce a spurious
    # "Invalid symbol" rejection even when the wizard supplied a valid
    # canonical symbol inside ``structured_request``.
    _structured = req.get("structured_request") if isinstance(req, Mapping) else None
    if not isinstance(_structured, Mapping):
        _structured = {}

    def _coalesce_field(field_name: str) -> Any:
        if not isinstance(req, Mapping):
            return None
        top_val = req.get(field_name)
        if top_val is not None and str(top_val) != "":
            return top_val
        sr_val = _structured.get(field_name)
        if sr_val is not None and str(sr_val) != "":
            return sr_val
        return None

    symbol = _normalize_symbol(_coalesce_field("symbol"))
    if symbol is None:
        return _execution_result(request, success=False, error="Invalid symbol (expected format: PERP_<BASE>_<QUOTE>)")

    side = _normalize_side(_coalesce_field("side"))
    if side is None:
        return _execution_result(request, success=False, error="Invalid side (expected buy or sell)")
    side = side.lower()

    creds = _resolve_account_credentials(account)
    if creds is None or not getattr(creds, 'account_id', ''):
        return _execution_result(request, success=False, error=f"Raydium account '{account}' not resolved (credentials missing or malformed)")

    client = client_factory() if callable(client_factory) else RaydiumHttpClient()
    try:
        open_payload = client._signed_request(creds=creds, method="GET", path="/v1/orders", params={"status": "INCOMPLETE"})
    except _RaydiumHttpError as exc:
        return _execution_result(request, success=False, error=str(exc), exchange_response={"status": exc.status_code, "payload": exc.payload})

    rows = []
    if isinstance(open_payload, Mapping):
        data = open_payload.get("data", {})
        if isinstance(data, Mapping):
            rows = list(data.get("rows") or [])
        elif isinstance(data, list):
            rows = data

    from .raydium_agent import _hermes_normalize_raydium_order

    normalized_orders = []
    for raw in rows:
        if not isinstance(raw, Mapping):
            continue
        o = _hermes_normalize_raydium_order(raw)
        if str(o.get("symbol") or "").upper() != symbol:
            continue
        if str(o.get("side") or "").lower() != side:
            continue
        if o.get("order_id") in (None, ""):
            continue
        normalized_orders.append(o)

    order_ids = [o.get("order_id") for o in normalized_orders]
    if not order_ids:
        return _execution_result(
            request,
            success=True,
            exchange_response={"raw": open_payload, "broker_id": BROKER_ID, "network": NETWORK},
            exchange="raydium",
            account=account,
            symbol=symbol,
            side=side,
            matched_before=0,
            attempted=0,
            cancelled=0,
            failed=0,
            order_ids_attempted=[],
            cancelled_order_ids=[],
            failed_order_ids=[],
            remaining_after=0,
            remaining_order_ids=[],
            partial=False,
            noop=True,
            verified_success=True,
            matched_order_count=0,
            verified_canceled_count=0,
            remaining_target_count=0,
        )

    attempted = []
    cancelled = []
    failed = []
    failure_error = None
    for order in normalized_orders:
        cancel_req = {"version": 1, "operation": "cancel_order", "exchange": "raydium", "account": account, "symbol": symbol, "order_id": order.get("order_id")}
        cancel_result = execute_cancel(cancel_req, accounts_resolver=accounts_resolver, client_factory=client_factory, sign_request_fn=sign_request_fn)
        if cancel_result.get("success"):
            cancelled.append(order.get("order_id"))
        else:
            failed.append(order.get("order_id"))
            if failure_error is None:
                failure_error = str(cancel_result.get("error") or cancel_result.get("message") or "cancel failed")
        attempted.append(order.get("order_id"))

    try:
        post_payload = client._signed_request(creds=creds, method="GET", path="/v1/orders", params={"status": "INCOMPLETE"})
    except _RaydiumHttpError as exc:
        return _execution_result(request, success=False, error=str(exc), exchange_response={"status": exc.status_code, "payload": exc.payload}, matched_before=len(order_ids), attempted=len(attempted), cancelled=len(cancelled), failed=len(failed), order_ids_attempted=attempted, cancelled_order_ids=cancelled, failed_order_ids=failed, remaining_after=None, remaining_order_ids=None, partial=bool(cancelled and failed), verified_success=False)

    post_rows = []
    if isinstance(post_payload, Mapping):
        pdata = post_payload.get("data", {})
        if isinstance(pdata, Mapping):
            post_rows = list(pdata.get("rows") or [])
        elif isinstance(pdata, list):
            post_rows = pdata

    from .raydium_agent import _hermes_normalize_raydium_order

    remaining = []
    for raw in post_rows:
        if not isinstance(raw, Mapping):
            continue
        o = _hermes_normalize_raydium_order(raw)
        if str(o.get("symbol") or "").upper() == symbol and str(o.get("side") or "").lower() == side and o.get("order_id") not in (None, ""):
            remaining.append(o.get("order_id"))

    verified_canceled = [oid for oid in cancelled if oid not in remaining]
    partial = bool(failed) or len(verified_canceled) < len(order_ids)
    success = not partial and not remaining and not failed
    return _execution_result(
        request,
        success=success,
        exchange_response={"raw": post_payload, "broker_id": BROKER_ID, "network": NETWORK},
        exchange="raydium",
        account=account,
        symbol=symbol,
        side=side,
        matched_before=len(order_ids),
        attempted=len(attempted),
        cancelled=len(verified_canceled),
        failed=len(failed),
        order_ids_attempted=attempted,
        cancelled_order_ids=verified_canceled,
        failed_order_ids=failed,
        remaining_after=len(remaining),
        remaining_order_ids=remaining,
        partial=partial,
        noop=False,
        verified_success=success,
        matched_order_count=len(order_ids),
        verified_canceled_count=len(verified_canceled),
        remaining_target_count=len(remaining),
        verification_status="complete" if success else ("partial" if verified_canceled else "failed"),
        error=None if success else failure_error or ("No matching open orders remain." if not order_ids else "Raydium grouped cancellation failed"),
    )
