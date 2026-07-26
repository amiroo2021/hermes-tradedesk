"""Interactive /trade wizard for Telegram.

The wizard owns Telegram UI/state only. Completed StructuredTradeRequests are
submitted to TradeDesk, and the returned ExecutionResult is rendered back to
Telegram.
"""
from __future__ import annotations

import copy
import json
import os
import re
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

try:  # pragma: no cover - exercised in gateway runtime
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
except Exception:  # pragma: no cover - lets pure unit tests import the module
    InlineKeyboardButton = None  # type: ignore[assignment]
    InlineKeyboardMarkup = None  # type: ignore[assignment]

from plugins.platforms.telegram.shared_selectors import (
    account_keyboard,
    account_prompt,
    exchange_keyboard,
    exchange_prompt,
    lighter_account_keyboard,
)

STATE_ATTR = "_trade_menu_state"
CALLBACK_PREFIX = "tm:"

SYMBOLS_PLACE: List[str] = ["BTC", "ETH", "SOL", "XRP"]
SYMBOLS_COMMON: List[str] = ["BTC", "ETH", "SOL"]

WORKFLOW_TITLES = {
    "place_order": "📈 Place Order",
    "ladder": "📊 Ladder",
    "position_manager": "💼 Position Manager",
    "open_orders": "📋 Open Orders",
    "balance": "💰 Balance",
    "cancel_orders": "❌ Cancel Orders",
}

NUMERIC_FIELDS = {
    "price",
    "size",
    "order_count",
    "total_volume",
    "start_price",
    "end_price",
}


@dataclass
class Screen:
    text: str
    keyboard: Any = None


def is_trade_command(text: str) -> bool:
    """Return True for /trade or /trade@BotName commands."""
    first = (text or "").strip().split(maxsplit=1)[0]
    return bool(re.fullmatch(r"/trade(?:@[A-Za-z0-9_]+)?", first))


def _state_map(adapter: Any) -> Dict[str, dict]:
    state = getattr(adapter, STATE_ATTR, None)
    if state is None:
        state = {}
        setattr(adapter, STATE_ATTR, state)
    return state


def _key_from_parts(chat_id: Any, user_id: Any) -> str:
    return f"{chat_id}:{user_id}"


def _key_from_message(msg: Any) -> str:
    return _key_from_parts(getattr(msg, "chat_id", None), getattr(getattr(msg, "from_user", None), "id", None))


def _key_from_query(query: Any) -> str:
    message = getattr(query, "message", None)
    chat_id = getattr(message, "chat_id", None)
    user_id = getattr(getattr(query, "from_user", None), "id", None)
    return _key_from_parts(chat_id, user_id)


def _callback(*parts: str) -> str:
    return CALLBACK_PREFIX + ":".join(parts)


def _raydium_display_symbol(symbol: Any) -> str:
    raw = str(symbol or "").upper()
    if raw.startswith("PERP_") and raw.endswith("_USDC") and len(raw) > len("PERP__USDC"):
        core = raw[len("PERP_"):-len("_USDC")]
        if core:
            return core
    return raw or "UNKNOWN"


def _button(label: str, data: str) -> Any:
    if InlineKeyboardButton is None:  # pragma: no cover
        raise RuntimeError("python-telegram-bot is required for trade_menu keyboards")
    return InlineKeyboardButton(label, callback_data=data)


def _markup(rows: List[List[Tuple[str, str]]]) -> Any:
    if InlineKeyboardMarkup is None:  # pragma: no cover
        raise RuntimeError("python-telegram-bot is required for trade_menu keyboards")
    return InlineKeyboardMarkup([[_button(label, data) for label, data in row] for row in rows])


def _nav_rows(include_submit: bool = False) -> List[List[Tuple[str, str]]]:
    if include_submit:
        return [[("✅ Submit", _callback("submit"))], [("⬅️ Back", _callback("back")), ("❌ Cancel", _callback("cancel"))]]
    return [[("⬅️ Back", _callback("back")), ("❌ Cancel", _callback("cancel"))]]


def _root_keyboard() -> Any:
    return _markup(
        [
            [("📈 Place Order", _callback("workflow", "place_order")), ("📊 Ladder", _callback("workflow", "ladder"))],
            [("📋 Open Orders", _callback("workflow", "open_orders")), ("💼 Manage Positions", _callback("workflow", "position_manager"))],
            [("💰 Balance", _callback("workflow", "balance")), ("❌ Cancel Orders", _callback("workflow", "cancel_orders"))],
        ]
    )


def _exchange_keyboard() -> Any:
    return exchange_keyboard(_callback, _nav_rows)


def _account_keyboard(data: Optional[dict] = None) -> Any:
    exchange = str((data or {}).get("exchange") or "").strip().lower()
    accounts, _error = _exchange_accounts(data or {})
    if exchange == "lighter":
        # Lighter accounts are returned as dicts by the agent so the
        # keyboard can render the instance label.
        entries = [a for a in (accounts or []) if isinstance(a, dict)]
        return lighter_account_keyboard(_callback, _nav_rows, entries)
    entries = [a for a in (accounts or []) if isinstance(a, (str, dict))]
    return account_keyboard(_callback, _nav_rows, accounts=entries)


def _symbol_keyboard(symbols: Iterable[str], include_all: bool = False) -> Any:
    buttons = [(sym, _callback("set", "symbol", sym)) for sym in symbols]
    rows = [buttons[i : i + 2] for i in range(0, len(buttons), 2)]
    if include_all:
        rows.append([("All Symbols", _callback("set", "symbol", "all"))])
    rows.append([("Other...", _callback("input", "symbol"))])
    rows.extend(_nav_rows())
    return _markup(rows)


def _choice_keyboard(field: str, choices: List[Tuple[str, str]]) -> Any:
    rows = [[(label, _callback("set", field, value))] for label, value in choices]
    rows.extend(_nav_rows())
    return _markup(rows)


def _preview_keyboard() -> Any:
    return _markup(_nav_rows(include_submit=True))


def _final_view_keyboard() -> Any:
    """Return the post-execute keyboard shown under read-only reports.

    This keyboard is the standard for any final state that displays a
    formatter result (balance, positions, open orders, etc.). The two
    affordances are:

      - **Back** (callback ``back``): pops the wizard's history stack
        and re-renders the previous screen. For a freshly opened wizard
        this returns to the root menu.
      - **Raw** (callback ``raw``): switches the screen into the raw
        debug view that prints the full JSON the agent returned. The raw
        view is exchange-specific and exists for debugging only.
      - **Cancel** (callback ``cancel``): clears state and returns to root.
    """
    rows = [
        [("Back", _callback("back")), ("Raw", _callback("raw"))],
        [("Cancel", _callback("cancel"))],
    ]
    return _markup(rows)


def _raw_view_text(state: dict) -> str:
    """Render the full exchange response for the raw debug view.

    The exchange agent returns a dict with two layers of debug data:

      - ``result["message"]`` — the formatted, exchange-agnostic
        string the user normally sees (the standard rendering).
      - ``result["data"]`` — the agent's full envelope, including
        ``exchange_response`` (the agent's normalized response) and
        ``raw_response`` (the exchange's raw bytes converted to a dict).
        This is exchange-specific and exists for debugging only.

    Both are included in the raw view so the operator can correlate
    the formatted message with the underlying exchange payload.
    """
    raw = state.get("_final_raw_result")
    if not isinstance(raw, Mapping):
        return "Raw response unavailable."
    # Truncate large nested payloads so the Telegram message stays readable.
    formatted = json.dumps(raw, indent=2, ensure_ascii=False, default=str)
    if len(formatted) > 3500:
        # Telegram's hard limit is 4096 chars; keep a safety margin.
        formatted = formatted[:3500] + "\n\n... (truncated; full payload in state['_final_raw_result'])"
    return formatted


def _positions_keyboard(positions: List[dict]) -> Any:
    rows = []
    for idx, pos in enumerate(positions):
        side = str(pos.get("side") or "").lower()
        emoji = "🔵" if side == "long" else "🔴"
        symbol = str(pos.get("display_symbol") or pos.get("symbol") or "?").upper()
        side_label = "Long" if side == "long" else "Short"
        rows.append([(f"{emoji} {symbol} {side_label} {_format_value(pos.get('size'))}", _callback("position", str(idx)))])
    rows.extend(_nav_rows())
    return _markup(rows)


def _position_actions_keyboard() -> Any:
    return _markup([[("Set TP", _callback("pos_action", "set_tp")), ("Set SL", _callback("pos_action", "set_sl"))]] + _nav_rows())


def _cancel_group_keyboard(groups: List[dict]) -> Any:
    """Build the inline keyboard for the cancel-group select screen.

    Raydium grouped cancel buttons are concise and grouped by canonical
    symbol + normalized side: ``🔵 ETH (1)`` / ``🔵 BTC (11)``.
    Other exchanges keep the richer legacy labels.
    """
    buttons = []
    for idx, group in enumerate(groups, start=1):
        if group.get("_is_cancel_all_sentinel"):
            buttons.append((f"{idx}️⃣ ❌ Cancel ALL open orders", _callback("cancel_group", str(idx - 1))))
            continue
        if group.get("_raydium_grouped_menu"):
            side = str(group.get("side") or "").lower()
            emoji = "🔵" if side == "buy" else "🔴" if side == "sell" else "⚪️"
            symbol = str(group.get("display_symbol") or group.get("symbol") or "UNKNOWN").upper()
            count = int(group.get("count") or 0)
            buttons.append((f"{emoji} {symbol} ({count})", _callback("cancel_group", str(idx - 1))))
            continue
        if group.get("exchange_symbol") and ("buy_count" in group or "sell_count" in group):
            symbol = str(group.get("display_symbol") or group.get("symbol") or group.get("exchange_symbol") or "UNKNOWN").upper()
            buy_count = int(group.get("buy_count") or 0)
            sell_count = int(group.get("sell_count") or 0)
            if buy_count > 0:
                buttons.append((_cancel_group_button_label(idx, {"symbol": symbol, "side": "buy", "count": buy_count, "total": None, "min_price": None, "max_price": None}), _callback("cancel_group", str(idx - 1))))
            if sell_count > 0:
                buttons.append((_cancel_group_button_label(idx, {"symbol": symbol, "side": "sell", "count": sell_count, "total": None, "min_price": None, "max_price": None}), _callback("cancel_group", str(idx - 1))))
            continue
        buttons.append((_cancel_group_button_label(idx, group), _callback("cancel_group", str(idx - 1))))
    rows = [buttons[i : i + 1] for i in range(0, len(buttons), 1)]
    rows.extend(_nav_rows())
    return _markup(rows)


def _new_state(workflow: Optional[str] = None, step: str = "root") -> dict:
    return {"workflow": workflow, "step": step, "data": {}, "history": [], "awaiting": None}


def _push(state: dict) -> None:
    snapshot = copy.deepcopy({k: v for k, v in state.items() if k != "history"})
    state.setdefault("history", []).append(snapshot)


def _restore_back(state: dict) -> bool:
    hist = state.get("history") or []
    if not hist:
        return False
    prev = hist.pop()
    state.clear()
    state.update(prev)
    state["history"] = hist
    return True


def _set_step(state: dict, step: str, *, push: bool = True, awaiting: Optional[str] = None) -> None:
    if push:
        _push(state)
    state["step"] = step
    state["awaiting"] = awaiting


def _set_data_and_advance(state: dict, field: str, value: Any) -> None:
    _push(state)
    state.setdefault("data", {})[field] = value
    next_step = _next_step(state["workflow"], field, state["data"])
    state["step"] = next_step
    state["awaiting"] = next_step if next_step in NUMERIC_FIELDS else None


def _next_step(workflow: str, field: str, data: dict) -> str:
    if field == "exchange":
        return "account"
    if field == "account":
        if workflow == "position_manager":
            return "position_select"
        if workflow in {"open_orders", "balance"}:
            return "final_display"
        if workflow == "cancel_orders":
            return "cancel_group_select"
        return "symbol"
    if workflow == "place_order":
        seq = {"symbol": "side", "side": "order_type", "price": "size", "size": "preview"}
        if field == "order_type":
            return "price" if data.get("order_type") == "limit" else "size"
        return seq[field]
    if workflow == "ladder":
        seq = {
            "symbol": "side",
            "side": "distribution",
            "distribution": "order_count",
            "order_count": "total_volume",
            "total_volume": "start_price",
            "start_price": "end_price",
            "end_price": "preview",
        }
        return seq[field]
    if workflow == "cancel_orders":
        seq = {"symbol": "side", "side": "order_type", "order_type": "preview"}
        return seq[field]
    return "root"


def _format_value(value: Any) -> str:
    if value == "all":
        return "All Symbols"
    return str(value)


def _normalize_raydium_symbol(value: Any) -> Optional[str]:
    try:
        from tradedesk.raydium_write import _normalize_symbol as _raydium_normalize_symbol
    except Exception:
        return None
    return _raydium_normalize_symbol(value)


def _title(text: str) -> str:
    return f"{text}\n\n"


def _exchange_accounts(data: dict) -> tuple[Optional[List[Any]], Optional[str]]:
    """Ask TradeDesk/exchange agent which accounts have trading credentials.

    Telegram remains UI-only: it asks TradeDesk, which routes to the selected
    exchange agent. No secret values are returned or rendered.

    Returns a heterogeneous list: ``str`` for exchanges whose
    ``list_accounts`` returns plain names (Hyperliquid, AFX, Pacifica,
    Apex), or ``dict`` entries ``{"account", "exchange_instance",
    "label"}`` for exchanges (Lighter) whose account discovery is
    instance-aware. The list is what is rendered by the account-selection
    keyboard; each rendering helper inspects the entry type.
    """
    exchange = str(data.get("exchange") or "").strip()
    if not exchange:
        return None, None
    try:
        from tradedesk.tradedesk import TradeDesk

        result = TradeDesk().list_accounts(exchange)
    except Exception as exc:
        return [], str(exc)
    if not isinstance(result, dict) or not result.get("success"):
        message = result.get("message") if isinstance(result, dict) else None
        error = result.get("error") if isinstance(result, dict) else None
        return [], str(message or error or f"No accounts found for {exchange}.")
    accounts = result.get("accounts")
    if not isinstance(accounts, list):
        return [], f"No accounts found for {exchange}."
    # Pass through whatever shape the agent returned: ``str`` for
    # name-only, ``dict`` for structured (account, instance) records.
    clean_accounts = [entry for entry in accounts
                      if isinstance(entry, (str, dict)) and (isinstance(entry, dict) or str(entry).strip())]
    return clean_accounts, None


def _account_selection_text(data: dict, error: Optional[str] = None) -> str:
    accounts, discovery_error = _exchange_accounts(data)
    exchange = str(data.get("exchange") or "Exchange")
    exchange_label = {"afx": "AFX", "hyperliquid": "Hyperliquid", "pacifica": "Pacifica"}.get(exchange.lower(), exchange.title())
    lines = [_summary(data), f"Select account for {exchange_label}:"]
    if error:
        lines.insert(0, f"⚠️ {error}\n")
    if discovery_error:
        lines.append(f"\n⚠️ {discovery_error}")
        lines.append("Use Other... to enter an account manually.")
    elif accounts is not None:
        lines.append(f"\nFound {len(accounts)} credentialed account(s).")
    return "\n".join(part for part in lines if part)


def _screen_for_state(state: dict, error: Optional[str] = None) -> Screen:
    workflow = state.get("workflow")
    step = state.get("step", "root")
    data = state.get("data", {})
    prefix = f"⚠️ {error}\n\n" if error else ""

    if step == "root":
        return Screen(prefix + "Trading Console\n\nChoose a workflow:", _root_keyboard())
    if step == "exchange":
        return Screen(prefix + _title(WORKFLOW_TITLES[str(workflow)]) + "Select exchange:", _exchange_keyboard())
    if step == "account":
        return Screen(_account_selection_text(data, error), _account_keyboard(data))
    if step == "cancel_group_select":
        return _cancel_group_select_screen(state, error)
    if step == "cancel_confirm":
        return _cancel_confirm_screen_for_state(state, error)
    if step == "symbol":
        symbols = SYMBOLS_PLACE if workflow == "place_order" else SYMBOLS_COMMON
        include_all = workflow == "cancel_orders"
        return Screen(prefix + _summary(data) + "\nSelect symbol:", _symbol_keyboard(symbols, include_all=include_all))
    if step == "side":
        choices = [("Buy", "buy"), ("Sell", "sell")]
        if workflow == "cancel_orders":
            choices.append(("Both", "both"))
        return Screen(prefix + _summary(data) + "\nSelect side:", _choice_keyboard("side", choices))
    if step == "order_type":
        choices = [("Limit", "limit"), ("Market", "market")]
        if workflow == "cancel_orders":
            choices.append(("All", "all"))
        return Screen(prefix + _summary(data) + "\nSelect order type:", _choice_keyboard("order_type", choices))
    if step == "distribution":
        return Screen(prefix + _summary(data) + "\nSelect distribution:", _choice_keyboard("distribution", [("Uniform", "uniform"), ("Half Gaussian", "half_gaussian")]))
    if step in NUMERIC_FIELDS:
        labels = {
            "price": "Enter price:",
            "size": "Enter size:",
            "order_count": "Enter order count:",
            "total_volume": "Enter total volume:",
            "start_price": "Enter start price:",
            "end_price": "Enter end price:",
        }
        return Screen(prefix + _summary(data) + "\n" + labels[step], _markup(_nav_rows()))
    if step == "await_exchange":
        return Screen(prefix + exchange_prompt(), _markup(_nav_rows()))
    if step == "await_account":
        return Screen(prefix + _summary(data) + "\n" + account_prompt(), _markup(_nav_rows()))
    if step == "await_symbol":
        return Screen(prefix + _summary(data) + "\nEnter symbol:", _markup(_nav_rows()))
    if step == "position_select":
        return _position_select_screen(state, error)
    if step == "position_action":
        return _position_action_screen(state, error)
    if step == "await_tp":
        return Screen(prefix + "Enter TP price.\n\nEnter 0 to remove TP.", _markup(_nav_rows()))
    if step == "await_sl":
        return Screen(prefix + "Enter SL price.\n\nEnter 0 to remove SL.", _markup(_nav_rows()))
    if step == "preview":
        return Screen(_preview_text(state), _preview_keyboard())
    if step == "final_display":
        return Screen(_final_display_text(state), _final_view_keyboard())
    if step == "raw_view":
        # Raw debug view of the most recent execution result. The Back
        # button pops the wizard history to the final_display screen.
        return Screen(_raw_view_text(state), _final_view_keyboard())
    return Screen(prefix + "Trading Console\n\nChoose a workflow:", _root_keyboard())


def _summary(data: dict) -> str:
    lines = []
    for field in ("exchange", "account", "symbol", "side", "order_type", "distribution"):
        if field in data:
            label = field.replace("_", " ").title()
            lines.append(f"{label}:\n{_format_value(data[field])}")
    return "\n\n".join(lines) + ("\n" if lines else "")


def _preview_text(state: dict) -> str:
    data = state.get("data", {})
    workflow = state.get("workflow")
    fields_by_workflow = {
        "place_order": ["exchange", "account", "symbol", "side", "order_type", "price", "size"],
        "ladder": ["exchange", "account", "symbol", "side", "distribution", "order_count", "total_volume", "start_price", "end_price"],
        "cancel_orders": ["exchange", "account", "symbol", "side", "order_type"],
    }
    lines = ["Trade Preview", ""]
    for field in fields_by_workflow.get(workflow, []):
        if field in data:
            lines.extend([field.replace("_", " ").title() + ":", _format_value(data[field]), ""])
    return "\n".join(lines).strip()


def _json_text(payload: dict) -> str:
    return json.dumps(payload, indent=2, ensure_ascii=False)


def _execute_trade_request(request: dict) -> dict:
    """Submit StructuredTradeRequest to TradeDesk and return its ExecutionResult."""
    try:
        from tradedesk.tradedesk import TradeDesk

        result = TradeDesk().execute(request)
    except Exception as exc:
        result = {
            "success": False,
            "message": f"❌ Trade request failed\n\n{exc}",
            "data": {
                "operation": request.get("operation"),
                "error": str(exc),
                "error_type": exc.__class__.__name__,
            },
        }
    return result if isinstance(result, dict) else {"success": False, "message": str(result), "data": {}}


def _render_trade_result(result: dict) -> str:
    debug = str(os.getenv("TRADE_MENU_DEBUG_JSON", "")).lower() in {"1", "true", "yes", "on"}
    if debug:
        return _json_text(result)
    if isinstance(result, dict) and result.get("message"):
        return str(result["message"])
    return _json_text(result)


def _execute_request_text(request: dict) -> str:
    """Submit StructuredTradeRequest to TradeDesk and render ExecutionResult."""
    return _render_trade_result(_execute_trade_request(request))


def _pending_cancel_groups(state: dict) -> tuple[List[dict], Optional[str]]:
    """Compute the per-(symbol, side) groups used by the cancel-orders
    wizard, plus a sentinel "Cancel ALL" entry.

    The wizard is exchange-agnostic: it operates only on the canonical
    ``orders`` list returned by every exchange's ``open_orders``
    dispatcher. Per-(symbol, side) grouping and the rich summary
    format are computed by ``TradeDesk._compute_open_orders_rich_summary``
    (the same renderer used by the read-only Open Orders view), so
    the wizard and the read-only view always agree on what is shown.

    The returned list always ends with a sentinel whose ``symbol`` is
    None and ``side`` is ``"all"``. The wizard renders this entry as
    the final "Cancel ALL open orders" button.

    Per the operator's spec, the entries include:
      - ``count``: number of open orders in the group
      - ``total``: sum of remaining open quantity (None if unavailable)
      - ``min_price``: minimum price in the group (None if unavailable)
      - ``max_price``: maximum price in the group (None if unavailable)
    """
    data = state.setdefault("data", {})
    cached = data.get("_cancel_groups")
    if isinstance(cached, list):
        return cached, data.get("_cancel_group_error")

    request = {
        "version": 1,
        "operation": "open_orders",
        "exchange": data.get("exchange"),
        "account": data.get("account"),
    }
    result = _execute_trade_request(request)
    payload = result.get("data") if isinstance(result, dict) else {}
    if not result.get("success"):
        message = (
            result.get("message")
            or (payload or {}).get("error")
            or "Could not fetch pending orders."
        )
        data["_cancel_groups"] = []
        data["_cancel_group_error"] = str(message)
        return [], str(message)

    orders = (payload or {}).get("orders") if isinstance(payload, dict) else []
    if not isinstance(orders, list):
        orders = []

    # Compute rich summary groups via the canonical TradeDesk helper.
    # This is the SAME renderer used by the read-only Open Orders
    # view, so the wizard and the read-only view always agree.
    # The helper may be unavailable in test environments that swap
    # out TradeDesk with a FakeTradeDesk; in that case we fall back
    # to a minimal grouping by (symbol, side) so the wizard remains
    # usable in tests.
    from tradedesk.tradedesk import TradeDesk
    summary_helper = getattr(TradeDesk, "_compute_open_orders_rich_summary", None)
    if callable(summary_helper):
        groups: list[dict] = list(summary_helper(orders))
    else:
        # Fallback grouping by (symbol, side) for test environments
        # that swap TradeDesk. This is never used in production where
        # TradeDesk._compute_open_orders_rich_summary is always
        # defined. Aggregates by (symbol, side) just like the
        # canonical helper.
        groups_by_key: dict[tuple[str, str], dict] = {}
        for order in orders:
            if not isinstance(order, dict):
                continue
            symbol = str(
                order.get("symbol")
                or order.get("coin")
                or order.get("display_symbol")
                or ""
            ).upper()
            side_raw = str(order.get("side") or "").strip().lower()
            side = (
                "buy" if side_raw in {"buy", "b", "bid", "long"} else
                "sell" if side_raw in {"sell", "s", "ask", "short"} else
                None
            )
            if not symbol or side is None:
                continue
            key = (symbol, side)
            bucket = groups_by_key.get(key)
            if bucket is None:
                bucket = {
                    "symbol": symbol,
                    "side": side,
                    "count": 0,
                    "total": None,
                    "min_price": None,
                    "max_price": None,
                }
                groups_by_key[key] = bucket
            bucket["count"] += 1
        groups = list(groups_by_key.values())

    # Apex uses an aggregated order_groups shape from the exchange
    # response. In that case the canonical ``orders`` list is empty
    # for Apex, but ``order_groups`` is present and represents the
    # pending orders. We surface those groups too, mapping Apex's
    # buy_count/sell_count into our rich-summary shape.
    apex_groups = (payload or {}).get("order_groups") or (payload or {}).get("symbol_groups")
    if str(data.get("exchange") or "").lower() == "apex" and isinstance(apex_groups, list):
        for g in apex_groups:
            if not isinstance(g, dict):
                continue
            symbol = str(
                g.get("display_symbol")
                or g.get("symbol")
                or g.get("exchange_symbol")
                or ""
            ).upper()
            if not symbol:
                continue
            buy_count = int(g.get("buy_count") or 0)
            sell_count = int(g.get("sell_count") or 0)
            if buy_count > 0:
                groups.append({
                    "symbol": symbol, "side": "buy",
                    "count": buy_count, "total": None,
                    "min_price": None, "max_price": None,
                    "_apex_group_index": len(groups),
                })
            if sell_count > 0:
                groups.append({
                    "symbol": symbol, "side": "sell",
                    "count": sell_count, "total": None,
                    "min_price": None, "max_price": None,
                    "_apex_group_index": len(groups),
                })

    # Sort groups lexicographically: (symbol, side) with buy-first.
    def _sort_key(item: dict) -> tuple[str, int]:
        side_priority = 0 if item.get("side") == "buy" else 1
        return (str(item.get("symbol") or ""), side_priority)

    groups.sort(key=_sort_key)

    frozen_group_orders: dict[tuple[str, str], list[dict[str, Any]]] = {}
    all_frozen_orders: list[dict[str, Any]] = []
    for order in orders if isinstance(orders, list) else []:
        if not isinstance(order, dict):
            continue
        symbol = str(
            order.get("display_symbol")
            or order.get("symbol")
            or order.get("coin")
            or ""
        ).upper()
        side_raw = str(order.get("side") or "").strip().lower()
        side = (
            "buy" if side_raw in {"buy", "b", "bid", "long"} else
            "sell" if side_raw in {"sell", "s", "ask", "short"} else
            None
        )
        order_id = order.get("order_id")
        market_id = order.get("market_id")
        resting_order_id = order.get("resting_order_id")
        # Raydium (Phase 2A.4D): accept orders that don't carry
        # market_id / resting_order_id / wide_order_id (those are
        # Apex/Hyperliquid/Lighter concepts). Only minimal identifier
        # fields are required for the exact-order cancel path.
        exchange_name = str(data.get("exchange") or "").lower()
        if exchange_name == "raydium":
            if (
                not symbol
                or side is None
                or order_id in {None, ""}
            ):
                continue
        else:
            if (
                not symbol
                or side is None
                or order_id in {None, ""}
                or market_id in {None, ""}
                or resting_order_id in {None, ""}
            ):
                continue
        # Phase 2A.4D (Raydium): also preserve price / quantity /
        # remaining_quantity so the per-order cancel rendering can
        # show useful numbers (Orderly orders carry "price" and
        # "quantity"; remaining_quantity falls back to "quantity").
        # For exchanges whose orders don't carry these fields we
        # simply omit them to preserve the historical frozen shape.
        frozen = {
            "order_id": order_id,
            "market_id": market_id,
            "resting_order_id": resting_order_id,
            "wide_order_id": order.get("wide_order_id"),
            "symbol": symbol,
            "side": side,
            "order_type": str(order.get("order_type") or "limit").lower(),
        }
        # Phase 2A.4D (Raydium): preserve raw "type" so the Open Orders
        # display can fall back from a missing/empty normalized
        # order_type to the exchange's raw value (Orderly emits
        # raw type="LIMIT" while the normalized order_type may be
        # empty in some exchanges).
        if order.get("type") not in (None, ""):
            frozen["raw_type"] = str(order.get("type")).strip().lower()
        # Phase 2A.4D: per-order Raydium cancel needs price + quantity;
        # preserve them when the source order carries them so the
        # Raydium cancel rendering can show them inline.
        _frozen_price = order.get("price") or order.get("limit_price") or order.get("avg_price")
        _frozen_qty = order.get("quantity") or order.get("size") or order.get("order_quantity")
        _frozen_remaining = order.get("remaining_quantity") or order.get("visible_quantity") or _frozen_qty
        if _frozen_price not in (None, ""):
            frozen["price"] = _frozen_price
        if _frozen_qty not in (None, ""):
            frozen["quantity"] = _frozen_qty
        if _frozen_remaining not in (None, ""):
            frozen["remaining_quantity"] = _frozen_remaining
        frozen_group_orders.setdefault((symbol, side), []).append(frozen)
        all_frozen_orders.append(frozen)

    for group in groups:
        symbol = str(group.get("symbol") or "").upper()
        side = str(group.get("side") or "").lower()
        group["orders"] = list(frozen_group_orders.get((symbol, side), []))
        group["scope_all_market"] = False

    # Raydium grouped cancel: group by canonical symbol + normalized side
    # and do not include a Cancel ALL sentinel at this UI level.
    if str(data.get("exchange") or "").lower() == "raydium":
        grouped: list[dict] = []
        buckets: dict[tuple[str, str], dict] = {}
        for frozen in all_frozen_orders:
            symbol = str(frozen.get("symbol") or "").upper()
            side = str(frozen.get("side") or "").lower()
            if not symbol or side not in {"buy", "sell"}:
                continue
            key = (symbol, side)
            group = buckets.get(key)
            if group is None:
                group = {
                    "symbol": symbol,
                    "display_symbol": _raydium_display_symbol(symbol),
                    "side": side,
                    "count": 0,
                    "total": None,
                    "min_price": None,
                    "max_price": None,
                    "orders": [],
                    "scope_all_market": False,
                    "_raydium_grouped_menu": True,
                }
                buckets[key] = group
                grouped.append(group)
            group["count"] += 1
            group["orders"].append(frozen)
        data["_cancel_groups"] = grouped
        data["_cancel_group_error"] = None
        return grouped, None

    # Add the "Cancel ALL" sentinel entry. The wizard renders this as
    # the final button. SKIPPED for Raydium per Phase 2A.4D operator
    # spec (cancel-all is forbidden on Raydium).
    groups.append({
        "symbol": None,
        "side": "all",
        "count": sum(int(g.get("count") or 0) for g in groups),
        "total": None,
        "min_price": None,
        "max_price": None,
        "_is_cancel_all_sentinel": True,
        "orders": list(all_frozen_orders),
        "scope_all_market": True,
    })

    data["_cancel_groups"] = groups
    data["_cancel_group_error"] = None
    return groups, None


def _cancel_group_button_label(idx: int, group: dict) -> str:
    """Render one numbered button label for a cancel-group option.

    Format matches the operator's spec: ``1️⃣ 🔵 BTC buy — 29 orders, total 8.23637, range 63500.0–63842.8``.
    For the Cancel ALL sentinel, the label is the operator's
    ``5️⃣ ❌ Cancel ALL open orders`` form.
    """
    if group.get("_is_cancel_all_sentinel"):
        return f"{idx}️⃣ ❌ Cancel ALL open orders"
    side = str(group.get("side") or "").lower()
    emoji = "🔵" if side == "buy" else "🔴" if side == "sell" else "⚪️"
    symbol = str(group.get("symbol") or "UNKNOWN").upper()
    count = int(group.get("count") or 0)
    verb = "order" if count == 1 else "orders"
    min_price = group.get("min_price")
    max_price = group.get("max_price")
    parts: list[str] = [f"{count} {verb}"]
    total = group.get("total_volume") or group.get("total")
    if total is not None:
        parts.append(f"total {total}")
    vwap = group.get("vwap")
    if vwap is not None:
        parts.append(f"VWAP {vwap}")
    if min_price is not None and max_price is not None:
        if min_price == max_price:
            parts.append(f"@ {min_price}")
        else:
            parts.append(f"range {min_price}–{max_price}")
    # Phase 2A.4D (Raydium): when the group is a Raydium single order
    # entry, render the exact price and remaining quantity inline so the
    # operator can identify the precise target before confirming. We never
    # expose credentials or signatures in this label.
    if group.get("_is_raydium_single_order") or group.get("order_id") is not None:
        orders_local = list(group.get("orders") or [])
        if orders_local:
            o = orders_local[0]
            price = o.get("price")
            qty = o.get("remaining_quantity") or o.get("quantity")
            details = []
            if price not in (None, ""):
                details.append(f"price {price}")
            if qty not in (None, ""):
                details.append(f"qty {qty}")
            if details:
                return f"{idx}️⃣ {emoji} {symbol} {side} — {count} {verb} — " + ", ".join(details)
    return f"{idx}️⃣ {emoji} {symbol} {side} — {', '.join(parts)}"


def _cancel_group_select_screen(state: dict, error: Optional[str] = None) -> Screen:
    """Step 1 of the cancel-orders wizard.

    Displays the rich Open Orders summary using the canonical
    per-(symbol, side) grouping, with numbered buttons (1️⃣, 2️⃣, ...)
    plus a final "Cancel ALL" button. The user selects one option to
    advance to the confirmation step.

    The summary text and grouping use the same canonical TradeDesk
    renderer as the read-only Open Orders view, so the wizard and the
    read-only view always agree on what is shown.
    """
    data = state.get("data", {})
    exchange_label = str(data.get("exchange") or "Exchange").title()
    account = str(data.get("account") or "")
    groups, fetch_error = _pending_cancel_groups(state)
    if fetch_error:
        prefix = f"⚠️ {error}\n\n" if error else ""
        return Screen(
            prefix
            + _summary(data)
            + "\nCould not query pending orders:\n"
            + str(fetch_error),
            _markup(_nav_rows()),
        )

    # Render the rich summary using the same canonical helper that the
    # read-only Open Orders view uses. This keeps the wizard and the
    # read-only view always in agreement.
    # The helper may be unavailable in test environments that swap
    # out TradeDesk with a FakeTradeDesk; in that case we render a
    # simpler fallback that uses the per-group summary directly.
    from tradedesk.tradedesk import TradeDesk

    # Re-compute the rich summary text using only the non-sentinel groups.
    non_sentinel = [g for g in groups if not g.get("_is_cancel_all_sentinel")]
    rich_renderer = getattr(TradeDesk, "_format_open_orders_rich_message", None)
    if callable(rich_renderer):
        summary_text = rich_renderer(
            [],
            exchange_label=exchange_label,
            account=account,
            total_count=int(data.get("_cancel_total_count") or 0)
            or sum(int(g.get("count") or 0) for g in non_sentinel),
        )
    else:
        # Test-environment fallback: render the summary text directly
        # from the per-group dicts. This branch is only reached when
        # the canonical TradeDesk helper is unavailable (e.g. when the
        # test suite has patched TradeDesk with a FakeTradeDesk).
        total_count = sum(int(g.get("count") or 0) for g in non_sentinel)
        lines = [
            f"📋 {exchange_label} Open Orders — {account}",
            "",
            f"Open orders: {total_count}",
        ]
        # We don't have direct rich-summary text in the test environment;
        # fall back to numbered-button labels only (the buttons convey
        # the rich info; the visible list above stays compact).
        summary_text = "\n".join(lines)

    # Build the visible list: numbered buttons + Cancel ALL.
    lines = [
        summary_text,
        "",
    ]

    prefix = f"⚠️ {error}\n\n" if error else ""

    if not non_sentinel:
        lines.append("No pending orders found for this exchange/account.")
        return Screen(prefix + "\n".join(lines), _markup(_nav_rows()))

    for idx, group in enumerate(groups, start=1):
        lines.append(_cancel_group_button_label(idx, group))

    lines.append("")
    lines.append("Choose an option to continue:")

    return Screen(prefix + "\n".join(lines), _cancel_group_keyboard(groups))


def _cancel_confirm_screen_for_state(state: dict, error: Optional[str] = None) -> Screen:
    """Resolve the selected group and render the confirmation dialog.

    Looks up the cached ``_pending_cancel_group_idx`` in the state's
    data, retrieves the corresponding group from the cached
    ``_cancel_groups`` list, and renders the confirmation screen.
    """
    base = state.setdefault("data", {})
    idx = base.get("_pending_cancel_group_idx")
    groups = base.get("_cancel_groups") or []
    if not isinstance(groups, list) or idx is None or idx < 0 or idx >= len(groups):
        return Screen(
            "Pending-order selection expired. Please choose again.",
            _markup(_nav_rows()),
        )
    return _cancel_confirm_screen(state, groups[idx], error)


def _cancel_confirm_screen(state: dict, group: dict, error: Optional[str] = None) -> Screen:
    """Step 2 of the cancel-orders wizard: confirmation dialog."""
    prefix = f"⚠️ {error}\n\n" if error else ""
    if group.get("_raydium_grouped_menu"):
        account = str(state.get("data", {}).get("account") or "")
        display_symbol = str(group.get("display_symbol") or group.get("symbol") or "UNKNOWN").upper()
        side = str(group.get("side") or "").lower()
        side_label = "Buy" if side == "buy" else "Sell" if side == "sell" else side.title()
        count = int(group.get("count") or 0)
        lines = [
            f"Raydium — {account}",
            "",
            display_symbol,
            f"Side: {side_label}",
            f"Matching orders: {count}",
            "",
            "Proceed?",
        ]
        buttons = [("✅ Yes", _callback("cancel_confirm_yes")), ("❌ No", _callback("cancel_confirm_no"))]
        return Screen(prefix + "\n".join(lines), _markup([buttons]))
    if group.get("_is_cancel_all_sentinel"):
        head = "Cancel all:\n\nALL OPEN ORDERS"
    else:
        side = str(group.get("side") or "").upper()
        symbol = str(group.get("symbol") or "UNKNOWN").upper()
        head = f"Cancel all:\n\n{symbol} {side}"
    lines = [head, ""]
    count = int(group.get("count") or 0)
    lines.append(f"Orders: {count}")
    lines.append("")
    total = group.get("total")
    lines.append(f"Total remaining: {total if total is not None else 'n/a'}")
    lines.append("")
    min_price = group.get("min_price")
    max_price = group.get("max_price")
    lines.append("Price range:")
    lines.append("")
    lines.append(f"{min_price}–{max_price}" if min_price is not None and max_price is not None else "n/a")
    lines.append("")
    lines.append("Proceed?")
    buttons = [("✅ Yes", _callback("cancel_confirm_yes")), ("❌ No", _callback("cancel_confirm_no"))]
    return Screen(prefix + "\n".join(lines), _markup([buttons]))


def _build_cancel_orders_request(data: dict, group: dict) -> dict:
    """Build the canonical TradeDesk cancel request for the selected group.

    The grouped cancel path is the project-standard ``cancel_orders``
    request for exchanges that support grouped cancellation. Raydium now
    uses this same grouped contract, preserving canonical symbol + side.

    The legacy exact single-order cancel flow remains available elsewhere
    via ``cancel_order``.

    Defense in depth: even if the upstream ``_pending_cancel_groups`` ever
    leaks a display-form symbol (e.g. ``ETH``) or non-canonical shorthand
    (e.g. ``ETH/USDC``) into the group dict, we re-normalize it through
    the exchange-specific canonicalizer before sending the request. The
    backend ``_normalize_symbol`` already accepts these shorthands, so the
    request never reaches the dispatch with an invalid symbol.
    """
    request: dict = {
        "version": 1,
        "operation": "cancel_orders",
        "exchange": data.get("exchange"),
        "account": data.get("account"),
    }
    raw_symbol = group.get("symbol")
    if raw_symbol:
        canonical_symbol = _wizard_canonicalize_symbol(
            raw_symbol, exchange=data.get("exchange")
        )
        if canonical_symbol:
            request["symbol"] = canonical_symbol
        else:
            # Fall back to the raw symbol; the backend helper will
            # surface the precise rejection reason if it's truly bad.
            request["symbol"] = raw_symbol
    raw_side = group.get("side")
    if raw_side and not group.get("_is_cancel_all_sentinel"):
        request["side"] = str(raw_side).strip().lower()
    if group.get("order_type"):
        request["order_type"] = str(group.get("order_type") or "limit").lower()
    if group.get("orders") is not None:
        request["orders"] = list(group.get("orders") or [])
    if group.get("scope_all_market") is not None:
        request["scope_all_market"] = bool(group.get("scope_all_market"))
    return request


def _wizard_canonicalize_symbol(symbol: Any, *, exchange: Any = None) -> Optional[str]:
    """Best-effort canonicalizer used by the wizard's cancel request builder.

    For Raydium we delegate to ``tradedesk.raydium_write._normalize_symbol``,
    which is the same helper the backend uses; that keeps the wizard and
    the backend in lockstep on what counts as a valid symbol. For other
    exchanges we upper-case the value as a minimum safe normalization;
    non-Raydium exchanges preserve their original behavior and are
    unaffected.

    Returns the canonical symbol string, or ``None`` if the value is
    empty/clearly malformed and cannot be salvaged.
    """
    if symbol is None:
        return None
    text = str(symbol).strip()
    if not text:
        return None
    if str(exchange or "").strip().lower() == "raydium":
        try:
            from tradedesk.raydium_write import _normalize_symbol
        except Exception:
            return text.upper() or None
        canonical = _normalize_symbol(text)
        if canonical:
            return canonical
        # Last resort: the backend helper will reject explicitly. We
        # return the upper-cased form so the user-facing error stays
        # deterministic instead of returning ``None``.
        return text.upper() or None
    return text.upper() or None


def _cancel_result_text(result: dict, group: dict) -> str:
    """Render the operator's specified result format from the canonical
    TradeDesk cancel_orders response.

    The ExchangeAgent returns a structured envelope with cancellation
    counts and a ``verification_status`` field. The wizard renders
    this directly into the operator's specified form.

    Per the operator's spec, the wizard MUST NOT collapse a
    "pending" verification into a misleading "✅ Cancel complete" or
    "⚠ Partial success" narrative. Submission success and verification
    are reported separately.

    Mapping from verification_status -> text:
      - "complete" -> ✅ Cancel complete
      - "pending"  -> ⏳ Cancellation submitted
      - "partial"  -> ⚠️ Cancellation partially completed
      - "failed"   -> ❌ Cancellation failed
      - "mismatch" -> ⚠️ Cancellation status requires review

    For legacy exchanges (AFX, Pacifica) that do not return
    ``verification_status``, the legacy rendering is preserved.
    """
    if not isinstance(result, dict):
        return f"❌ Cancel failed: {result!r}"
    if not result.get("success"):
        # Outer failure: prefer the TradeDesk-formatted message if
        # present (e.g. for chunked-cancel exchanges it carries the
        # full diagnostic context); otherwise synthesize.
        return result.get("message") or f"❌ Cancel failed: {result.get('error') or 'unknown error'}"

    data = (result.get("data") or {})

    # New result-classification path: when the envelope carries a
    # verification_status, render each state explicitly per spec.
    verification_status = data.get("verification_status")
    if verification_status in {
        "complete", "pending", "partial", "failed", "mismatch"
    }:
        account = data.get("account") or ""
        requested = int(data.get("requested_count") or data.get("matched_order_count") or 0)
        accepted = int(data.get("exchange_accepted_count") or 0)
        verified = int(data.get("verified_canceled_count") or 0)
        remaining = int(data.get("remaining_target_count") or max(requested - verified, 0))
        # ``exchange`` is lowercased elsewhere; preserve case for display.
        exchange_label = str(data.get("exchange") or "").title() or "Exchange"

        if verification_status == "complete":
            return (
                f"✅ Cancel complete\n\n"
                f"Requested: {requested}\n"
                f"Cancelled: {verified}\n"
                f"Remaining: 0"
            )
        if verification_status == "pending":
            # Submission was clean; bounded post-read hasn't confirmed
            # propagation yet. Do NOT show "Cancel complete" or
            # "Partial success".
            return (
                f"⏳ Cancellation submitted\n\n"
                f"Requested: {requested}\n"
                f"Exchange accepted: {accepted}\n"
                f"Verification: pending"
            )
        if verification_status == "partial":
            err = data.get("error") or data.get("stopped_error")
            tail = f"\nReason: {err}" if err else ""
            return (
                f"⚠️ Cancellation partially completed\n\n"
                f"Requested: {requested}\n"
                f"Exchange accepted: {accepted}\n"
                f"Verified cancelled: {verified}\n"
                f"Remaining: {remaining}{tail}"
            )
        if verification_status == "failed":
            err = data.get("error") or data.get("stopped_error") or "submission failed"
            return (
                f"❌ Cancellation failed\n\n"
                f"Requested: {requested}\n"
                f"Exchange accepted: {accepted}\n"
                f"Reason: {err}"
            )
        if verification_status == "mismatch":
            err = data.get("error") or data.get("stopped_error") or "result counts could not be reconciled"
            return (
                f"⚠️ Cancellation status requires review\n\n"
                f"Requested: {requested}\n"
                f"Exchange accepted: {accepted}\n"
                f"Verified cancelled: {verified}\n"
                f"Remaining: {remaining}\n"
                f"Reason: {err}"
            )

    # Legacy path: exchanges without verification_status.
    matched = data.get("matched_order_count")
    verified = data.get("verified_canceled_count")
    canceled = data.get("canceled_count")
    exchange_accepted = data.get("exchange_accepted_count")
    # Fall back: matched → requested (count from the group), canceled → max
    if matched is None:
        matched = int(group.get("count") or 0) if group else 0
    if verified is None and canceled is None and exchange_accepted is None:
        # Nothing diagnostic to report — assume success.
        return (
            f"✅ Cancel complete\n\n"
            f"Requested: {matched}\n"
            f"Cancelled: {matched}\n"
            f"Remaining: 0"
        )
    # Prefer verified_canceled_count when present.
    actual_canceled = (
        verified
        if verified is not None
        else canceled
        if canceled is not None
        else exchange_accepted
        if exchange_accepted is not None
        else matched
    )
    remaining = max(matched - actual_canceled, 0)
    if remaining == 0:
        return (
            f"✅ Cancel complete\n\n"
            f"Requested: {matched}\n"
            f"Cancelled: {actual_canceled}\n"
            f"Remaining: 0"
        )
    return (
        f"⚠ Partial success\n\n"
        f"Requested: {matched}\n"
        f"Cancelled: {actual_canceled}\n"
        f"Remaining: {remaining}"
    )


def _fetch_positions_for_trade(state: dict) -> List[dict]:
    data = state.setdefault("data", {})
    request = {"operation": "positions", "exchange": data.get("exchange"), "account": data.get("account")}
    result = _execute_trade_request(request)
    payload = result.get("data") if isinstance(result, dict) else {}
    positions = (payload or {}).get("positions") if isinstance(payload, dict) else []
    clean = []
    for pos in positions if isinstance(positions, list) else []:
        if not isinstance(pos, dict):
            continue
        try:
            if abs(float(pos.get("size") or 0)) <= 0:
                continue
        except Exception:
            continue
        clean.append(pos)
    state["positions"] = clean
    state["positions_error"] = None if result.get("success") else str(result.get("message") or (payload or {}).get("error") or "Failed to fetch positions")
    return clean


def _selected_position(state: dict) -> Optional[dict]:
    positions = state.get("positions") or []
    idx = state.get("selected_index")
    if isinstance(idx, int) and 0 <= idx < len(positions):
        return positions[idx]
    return None


def _position_select_screen(state: dict, error: Optional[str] = None) -> Screen:
    positions = _fetch_positions_for_trade(state)
    prefix = f"⚠️ {error}\n\n" if error else ""
    if state.get("positions_error"):
        return Screen(prefix + str(state.get("positions_error")), _markup(_nav_rows()))
    # Phase 43: render the per-position summary cards BEFORE the keyboard
    # so the operator can see size / entry / PnL / TP / SL before selecting
    # a position. The presentation helper is shared with positions_menu so
    # the two flows stay visually consistent.
    from plugins.platforms.telegram._positions_render import (
        positions_screen_text as _positions_screen_text,
    )
    data = state.get("data") or {}
    text = _positions_screen_text(
        exchange=data.get("exchange"),
        account=data.get("account"),
        positions=list(positions) if positions else [],
        error=state.get("positions_error"),
    )
    # The helper already handles error / empty states. We layer the
    # optional ``error`` prefix (set by callers via the function arg)
    # on top of the helper output.
    return Screen(prefix + text, _positions_keyboard(positions))


def _position_action_screen(state: dict, error: Optional[str] = None) -> Screen:
    pos = _selected_position(state)
    if not pos:
        return Screen("Position not found.", _markup(_nav_rows()))
    side_label = str(pos.get("side") or "").title()
    text = f"{str(pos.get('display_symbol') or pos.get('symbol') or '').upper()} {side_label} {_format_value(pos.get('size'))}\n\nChoose action:"
    return Screen((f"⚠️ {error}\n\n" if error else "") + text, _position_actions_keyboard())


def _final_display_text(state: dict) -> str:
    """Render the final display text.

    For flows that pre-store their result via ``state["_final_text"]``
    (e.g. the new cancel-orders wizard, which renders the operator's
    "✅ Cancel complete / ⚠ Partial success" format), the cached text
    is used. Otherwise the existing build/execute pattern is used.
    """
    cached = state.get("_final_text")
    if cached:
        return cached
    return _execute_request_text(_build_request(state))


def _number(text: str, *, integer: bool = False) -> Optional[Any]:
    value = (text or "").strip()
    try:
        if integer:
            if not re.fullmatch(r"[+-]?\d+", value):
                return None
            return int(value)
        n = float(value)
        return int(n) if n.is_integer() else n
    except Exception:
        return None


def _build_request(state: dict) -> dict:
    workflow = state.get("workflow")
    data = state.get("data", {})
    if workflow == "place_order":
        symbol = data.get("symbol")
        if str(data.get("exchange") or "").strip().lower() == "raydium":
            normalized = _normalize_raydium_symbol(symbol)
            if normalized is not None:
                symbol = normalized
        payload = {
            "version": 1,
            "operation": "place_order",
            "exchange": data.get("exchange"),
            "account": data.get("account"),
            "symbol": symbol,
            "side": data.get("side"),
            "order_type": data.get("order_type"),
        }
        if data.get("order_type") == "limit":
            payload["price"] = data.get("price")
        payload["size"] = data.get("size")
        return payload
    if workflow == "ladder":
        return {
            "version": 1,
            "operation": "ladder",
            "exchange": data.get("exchange"),
            "account": data.get("account"),
            "symbol": data.get("symbol"),
            "side": data.get("side"),
            "distribution": data.get("distribution"),
            "order_count": data.get("order_count"),
            "total_volume": data.get("total_volume"),
            "start_price": data.get("start_price"),
            "end_price": data.get("end_price"),
        }
    if workflow == "cancel_orders":
        return {
            "version": 1,
            "operation": "cancel_orders",
            "exchange": data.get("exchange"),
            "account": data.get("account"),
            "symbol": data.get("symbol"),
            "side": data.get("side"),
            "order_type": data.get("order_type"),
        }
    if workflow == "open_orders":
        return {"version": 1, "operation": "open_orders", "exchange": data.get("exchange"), "account": data.get("account")}
    if workflow == "balance":
        return {"version": 1, "operation": "balance", "exchange": data.get("exchange"), "account": data.get("account")}
    return {"version": 1, "operation": workflow or "unknown"}


async def _reply(msg: Any, screen: Screen) -> None:
    await msg.reply_text(screen.text, reply_markup=screen.keyboard, parse_mode=None)


async def _edit(query: Any, screen: Screen) -> None:
    try:
        await query.edit_message_text(text=screen.text, reply_markup=screen.keyboard, parse_mode=None)
    except Exception:
        # Some Telegram messages cannot be edited (old/deleted/etc.); answer by
        # sending a fresh message to preserve the wizard flow.
        message = getattr(query, "message", None)
        if message is not None:
            await message.reply_text(screen.text, reply_markup=screen.keyboard, parse_mode=None)


async def _send_root_and_clear(adapter: Any, query_or_msg: Any, key: str) -> None:
    _state_map(adapter).pop(key, None)
    screen = _screen_for_state(_new_state())
    if hasattr(query_or_msg, "edit_message_text"):
        await _edit(query_or_msg, screen)
    else:
        await _reply(query_or_msg, screen)


async def handle_trade_command(adapter: Any, msg: Any) -> bool:
    """Start a fresh /trade wizard, discarding prior state for chat_id:user_id."""
    if not is_trade_command(getattr(msg, "text", "")):
        return False
    key = _key_from_message(msg)
    state = _new_state()
    _state_map(adapter)[key] = state
    await _reply(msg, _screen_for_state(state))
    return True


async def handle_trade_text(adapter: Any, msg: Any) -> bool:
    """Capture typed Other... and numeric inputs for an active wizard."""
    key = _key_from_message(msg)
    states = _state_map(adapter)
    state = states.get(key)
    if not state or not state.get("awaiting"):
        return False

    field = state["awaiting"]
    text = (getattr(msg, "text", "") or "").strip()
    if field in {"tp", "sl"}:
        value = _number(text)
        if value is None:
            await _reply(msg, _screen_for_state(state, "Invalid number. Please enter a numeric value."))
            return True
        pos = _selected_position(state) or {}
        request = {
            "operation": "set_tp" if field == "tp" else "set_sl",
            "exchange": state.get("data", {}).get("exchange"),
            "account": state.get("data", {}).get("account"),
            "symbol": pos.get("symbol"),
            "side": pos.get("side"),
            "price": value,
            "position": dict(pos),
        }
        result = _execute_trade_request(request)
        states.pop(key, None)
        await _reply(msg, Screen(_render_trade_result(result), None))
        return True

    if field in NUMERIC_FIELDS:
        parsed = _number(text, integer=(field == "order_count"))
        if parsed is None:
            await _reply(msg, _screen_for_state(state, "Invalid number. Please enter a numeric value."))
            return True
        _set_data_and_advance(state, field, parsed)
    else:
        if not text:
            await _reply(msg, _screen_for_state(state, "Value cannot be empty. Please type a value."))
            return True
        _set_data_and_advance(state, field, text)

    if state.get("step") == "final_display":
        await _reply(msg, _screen_for_state(state))
        states.pop(key, None)
        return True
    await _reply(msg, _screen_for_state(state))
    return True


async def handle_trade_callback(adapter: Any, query: Any, data: str) -> bool:
    if not data.startswith(CALLBACK_PREFIX):
        return False
    await query.answer()
    parts = data[len(CALLBACK_PREFIX) :].split(":")
    action = parts[0]
    key = _key_from_query(query)
    states = _state_map(adapter)
    state = states.get(key) or _new_state()
    states[key] = state

    if action == "cancel":
        await _send_root_and_clear(adapter, query, key)
        return True

    if action == "back":
        if not _restore_back(state):
            state = _new_state()
            states[key] = state
        await _edit(query, _screen_for_state(state))
        return True

    if action == "workflow" and len(parts) == 2:
        state = _new_state(parts[1], "exchange")
        states[key] = state
        await _edit(query, _screen_for_state(state))
        return True

    if action == "input" and len(parts) == 2:
        field = parts[1]
        step = f"await_{field}" if field in {"exchange", "account", "symbol"} else field
        _set_step(state, step, awaiting=field)
        await _edit(query, _screen_for_state(state))
        return True

    if action == "set" and len(parts) >= 3:
        field = parts[1]
        value = ":".join(parts[2:])
        _set_data_and_advance(state, field, value)
        if state.get("step") == "final_display":
            await _edit(query, _screen_for_state(state))
            states.pop(key, None)
            return True
        await _edit(query, _screen_for_state(state))
        return True

    if action == "cancel_group" and len(parts) == 2:
        try:
            idx = int(parts[1])
        except Exception:
            await _edit(query, _screen_for_state(state, "Invalid pending-order selection."))
            return True
        groups, fetch_error = _pending_cancel_groups(state)
        if fetch_error:
            await _edit(query, _screen_for_state(state))
            return True
        if idx < 0 or idx >= len(groups):
            await _edit(query, _screen_for_state(state, "Pending-order selection expired. Please choose again."))
            return True
        # Step 1 → Step 2: freeze the exact selected group in state and
        # advance to the confirmation screen. The cancel is NOT executed here.
        base = state.setdefault("data", {})
        frozen_group = copy.deepcopy(groups[idx])
        base["_pending_cancel_group_idx"] = idx
        base["_pending_cancel_group"] = frozen_group
        state["step"] = "cancel_confirm"
        state["awaiting"] = None
        await _edit(query, _screen_for_state(state))
        return True

    if action == "cancel_confirm_yes":
        # Step 2 → Step 3: build the canonical cancel_orders request
        # for the selected group, submit exactly once, and transition
        # to the final display.
        base = state.setdefault("data", {})
        idx = base.get("_pending_cancel_group_idx")
        groups, fetch_error = _pending_cancel_groups(state)
        if fetch_error:
            await _edit(query, _screen_for_state(state))
            return True
        if idx is None or idx < 0 or idx >= len(groups):
            await _edit(query, _screen_for_state(state, "Pending-order selection expired. Please choose again."))
            return True
        group = base.get("_pending_cancel_group") if isinstance(base.get("_pending_cancel_group"), dict) else groups[idx]
        request = _build_cancel_orders_request(base, group)
        result = _execute_trade_request(request)
        text = _cancel_result_text(result, group)
        # Clear the cached groups so the next visit re-fetches.
        base.pop("_cancel_groups", None)
        base.pop("_cancel_group_error", None)
        base.pop("_pending_cancel_group_idx", None)
        base.pop("_pending_cancel_group", None)
        state["step"] = "final_display"
        state["awaiting"] = None
        state["_final_text"] = text
        state["_final_request"] = request
        state["_final_result"] = result
        await _edit(query, _screen_for_state(state))
        return True


    if action == "cancel_confirm_no":
        # Step 2 → Step 1: return to the cancel-group select screen.
        base = state.setdefault("data", {})
        base.pop("_pending_cancel_group_idx", None)
        state["step"] = "cancel_group_select"
        state["awaiting"] = None
        await _edit(query, _screen_for_state(state))
        return True

    if action == "position" and len(parts) >= 2:
        _push(state)
        try:
            state["selected_index"] = int(parts[1])
        except Exception:
            state["selected_index"] = None
        state["step"] = "position_action"
        state["awaiting"] = None
        await _edit(query, _screen_for_state(state))
        return True

    if action == "pos_action" and len(parts) == 2:
        _push(state)
        if parts[1] == "set_tp":
            state["step"] = "await_tp"
            state["awaiting"] = "tp"
        elif parts[1] == "set_sl":
            state["step"] = "await_sl"
            state["awaiting"] = "sl"
        await _edit(query, _screen_for_state(state))
        return True

    if action == "submit":
        # Capture the full result so the operator can later switch to the
        # raw-response debug view via the "Raw" button. ``result`` is the
        # exact dict returned by ``TradeDesk.execute`` — it includes both
        # the formatted ``message`` (already exchange-agnostic) and the
        # full ``data`` envelope with ``exchange_response`` and
        # ``raw_response`` from the agent (debug payload, exchange-specific).
        result = _execute_trade_request(_build_request(state))
        state["_final_text"] = _render_trade_result(result)
        state["_final_raw_result"] = result
        state["step"] = "final_display"
        await _edit(query, Screen(state["_final_text"], _final_view_keyboard()))
        return True

    if action == "raw":
        # Render the raw response. The agent has already separated the
        # formatted exchange-agnostic message (``result["message"]``)
        # from the debug payload (``result["data"]["exchange_response"]``
        # and ``result["data"]["raw_response"]``). The wizard reads
        # these from ``state["_final_raw_result"]`` so we can navigate
        # back to the formatted view without re-executing the request.
        _push(state)
        state["step"] = "raw_view"
        await _edit(query, _screen_for_state(state))
        return True

    return True


# Export pure helpers for tests/documentation generation.
build_request = _build_request
screen_for_state = _screen_for_state
new_state = _new_state
set_data_and_advance = _set_data_and_advance
