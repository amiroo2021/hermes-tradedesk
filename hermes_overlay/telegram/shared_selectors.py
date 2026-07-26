"""Reusable Telegram selector components for trading workflows.

Selectors are workflow-agnostic: they render choices and callback payloads only.
The calling wizard owns state transitions and resumes its workflow after a
selection is returned.
"""
from __future__ import annotations

from typing import Any, Callable, List, Tuple

try:  # pragma: no cover - gateway runtime
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
except Exception:  # pragma: no cover - pure smoke tests import without PTB
    InlineKeyboardButton = None  # type: ignore[assignment]
    InlineKeyboardMarkup = None  # type: ignore[assignment]

EXCHANGE_CHOICES: List[Tuple[str, str]] = [
    ("Hyperliquid", "hyperliquid"),
    ("AFX", "afx"),
    ("Pacifica", "pacifica"),
    ("Apex", "apex"),
    ("Lighter", "lighter"),
    ("Rise", "rise"),
    ("Raydium", "raydium"),
]
# Changed in Phase 2A.4D: Raydium added to the /trade exchange selector.
# Raydium accounts are surfaced through TradeDesk.list_accounts('raydium'),
# which delegates to RaydiumAgent.discover_raydium_accounts(). At least one
# credentialed alias must be resolvable from RAYDIUM_<ALIAS>_* env vars.
# No hardcoded account list for Raydium here; the wizard calls
# TradeDesk.list_accounts() at runtime, matching all other exchanges that
# discover accounts dynamically.
# Raydium accounts are surfaced through TradeDesk.list_accounts('raydium'),
# which delegates to RaydiumAgent.discover_raydium_accounts(). At least one
# credentialed alias must be resolvable from RAYDIUM_<ALIAS>_* env vars.
# No hardcoded account list for Raydium here; the wizard calls
# TradeDesk.list_accounts() at runtime, matching all other exchanges that
# discover accounts dynamically.

ACCOUNT_CHOICES: List[str] = [
    "EXAMPLE",
    "ACCOUNT1",
    "ACCOUNT2",
    "ACCOUNT3",
    "ACCOUNT4",
    "EXAMPLE5",
    "EXAMPLE6",
    "EXAMPLE7",
]


def exchange_keyboard(
    callback: Callable[..., str],
    nav_rows: Callable[[], List[List[Tuple[str, str]]]],
) -> Any:
    """Render workflow-agnostic exchange selection keyboard."""
    if InlineKeyboardButton is None or InlineKeyboardMarkup is None:  # pragma: no cover
        raise RuntimeError("python-telegram-bot is required for exchange selector keyboards")
    rows: List[List[Tuple[str, str]]] = [[(label, callback("set", "exchange", value))] for label, value in EXCHANGE_CHOICES]
    rows.append([("Other...", callback("input", "exchange"))])
    rows.extend(nav_rows())
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(label, callback_data=data) for label, data in row] for row in rows]
    )


def exchange_prompt() -> str:
    return "Enter exchange name:"


def account_keyboard(
    callback: Callable[..., str],
    nav_rows: Callable[[], List[List[Tuple[str, str]]]],
    accounts: List[str | dict] | None = None,
) -> Any:
    """Render workflow-agnostic account selection keyboard.

    The caller supplies its callback builder and nav rows, so this component has
    no knowledge of which workflow or plugin invoked it.
    """
    if InlineKeyboardButton is None or InlineKeyboardMarkup is None:  # pragma: no cover
        raise RuntimeError("python-telegram-bot is required for account selector keyboards")
    choices = accounts if accounts is not None else ACCOUNT_CHOICES
    rows: List[List[Tuple[str, str]]] = []
    for entry in choices:
        if isinstance(entry, dict):
            account = str(entry.get("account") or "").strip()
            if not account:
                continue
            label = str(entry.get("label") or account.upper())
            rows.append([(label, callback("set", "account", account))])
            continue
        account = str(entry).strip()
        if not account:
            continue
        rows.append([(account, callback("set", "account", account))])
    rows.append([("Other...", callback("input", "account"))])
    rows.extend(nav_rows())
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(label, callback_data=data) for label, data in row] for row in rows]
    )


def account_prompt() -> str:
    """Prompt used by callers for the free-form Other... account path."""
    return "Enter account name:"


def lighter_account_keyboard(
    callback: Callable[..., str],
    nav_rows: Callable[[], List[List[Tuple[str, str]]]],
    accounts: List[dict],
) -> Any:
    """Render the Lighter-specific account-selection keyboard.

    Each row shows ``"<account> — <chain label>"`` (e.g. ``EXAMPLE —
    Arbitrum``) and uses the standard ``set:account:<name>`` callback.
    The chain is NOT carried in the callback payload — the
    ``LighterAgent`` reads ``LIGHTER_<account>_CHAIN`` from the
    operator's environment when the request is executed.

    ``accounts`` is a list of dicts with keys ``account``, ``chain``,
    and ``label`` — the canonical shape returned by
    ``LighterAgent.list_accounts()``. This helper is the only place
    in the codebase that knows about the Lighter chain label.
    """
    if InlineKeyboardButton is None or InlineKeyboardMarkup is None:  # pragma: no cover
        raise RuntimeError("python-telegram-bot is required for Lighter account selector keyboards")
    rows: List[List[Tuple[str, str]]] = [
        [(entry.get("label") or entry.get("account"),
          callback("set", "account", str(entry.get("account"))))
         for entry in accounts]
    ]
    rows.append([("Other...", callback("input", "account"))])
    rows.extend(nav_rows())
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(label, callback_data=data) for label, data in row] for row in rows]
    )
