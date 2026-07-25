"""Hyperliquid exchange-specific executor.

This module is the only place that talks to the Hyperliquid Python SDK. TradeDesk
owns strategy/normalization; HyperliquidAgent only executes normalized exchange
requests.
"""
from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Any, Mapping, Optional

from .account_discovery import discover_accounts
from .request_utils import _request_field

# Lazy SDK imports: the hyperliquid-python-sdk is optional at runtime.
try:
    from eth_account import Account
    from hyperliquid.exchange import Exchange
    from hyperliquid.info import Info
except Exception:  # pragma: no cover - exercised when dependency is missing
    Account = None  # type: ignore[assignment]
    Exchange = None  # type: ignore[assignment]
    Info = None  # type: ignore[assignment]  # noqa: F821

SUPPORTED_OPERATIONS = {"order", "batch_orders", "cancel_orders", "balance", "open_orders", "positions", "set_tp", "set_sl"}
MAINNET_API_URL = "https://api.hyperliquid.xyz"

# Per-call cap for Hyperliquid bulk_cancel.
#
# Hyperliquid currently rejects a single signed action whose weight exceeds
# 200 ("Signed action over weight limit of 200."). We use the maximum
# allowed weight (200) so the largest chunk we ever submit is also the
# largest the exchange will accept; this keeps the implementation aligned
# with the exchange's current maximum supported weight for a cancel action.
# Reduce this constant only if Hyperliquid changes the limit downward; in
# that case bump it conservatively below the new cap.
HYPERLIQUID_CANCEL_CHUNK_SIZE = 200

# Per-call cap for Hyperliquid bulk_orders (place-orders path).
#
# Same exchange-level constraint as the cancel path: a single signed
# ``order`` action whose weight exceeds 200 is rejected atomically. We
# keep placement and cancellation independently configurable so that
# future exchange-side changes can be applied to one without disturbing
# the other. The 200 cap matches Hyperliquid's current maximum supported
# weight for a single signed action; reduce this constant only if the
# exchange lowers the cap.
HYPERLIQUID_ORDER_CHUNK_SIZE = 200

logger = logging.getLogger(__name__)


def _execution_result(
    request: Mapping[str, Any], *, success: bool, error: Optional[str] = None, **extra: Any
) -> dict:
    result = {
        "success": success,
        "exchange": "hyperliquid",
        "operation": request.get("operation"),
        "parent_operation": request.get("parent_operation"),
        "account": request.get("account"),
    }
    if error:
        result["error"] = error
    result.update(extra)
    return result


def _hermes_env_path() -> Path:
    """Return the active Hermes .env path without reading or logging secrets."""
    home = os.getenv("HERMES_HOME")
    hermes_home = Path(home).expanduser() if home else Path.home() / ".hermes"
    return hermes_home / ".env"


def _strip_dotenv_value(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def _dotenv_casefold_map() -> dict[str, tuple[str, str]]:
    """Load ~/.hermes/.env as case-insensitive key -> (actual key, value).

    Values are returned to the caller but never logged by this module.
    """
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
        if not key:
            continue
        value = _strip_dotenv_value(value)
        if value.strip():
            out[key.lower()] = (key, value.strip())
    return out


def _combined_casefold_env() -> dict[str, tuple[str, str, str]]:
    """Case-insensitive view of process env + .env.

    Source is one of "environment" or "dotenv". Process env wins when already
    set; otherwise credentials are read directly from the existing Hermes .env.
    """
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
    """Build searched variable names dynamically from the selected account."""
    names: list[str] = []
    segment = _account_segment(account)
    if segment:
        names.append(f"HYPERLIQUID_{segment}_{kind}")
    names.append(f"HYPERLIQUID_{kind}")
    return names


def _lookup_case_insensitive(names: list[str]) -> tuple[Optional[str], Optional[str], Optional[str], list[str]]:
    available = _combined_casefold_env()
    for name in names:
        found = available.get(name.lower())
        if found:
            actual_key, value, source = found
            return value, actual_key, source, names
    return None, None, None, names


def _resolve_wallet(account: Optional[str]) -> tuple[Optional[str], Optional[str], list[str]]:
    value, selected_key, source, searched = _lookup_case_insensitive(_candidate_names(account, "WALLET"))
    if selected_key:
        logger.info("Hyperliquid selected wallet variable %s from %s", selected_key, source)
    return value, selected_key, searched


def _resolve_secret(account: Optional[str]) -> tuple[Optional[str], Optional[str], list[str]]:
    value, selected_key, source, searched = _lookup_case_insensitive(_candidate_names(account, "SECRET"))
    if selected_key:
        logger.info("Hyperliquid selected secret variable %s from %s", selected_key, source)
    return value, selected_key, searched


def _missing_credentials_error(account: Optional[str], searched: list[str]) -> RuntimeError:
    return RuntimeError(
        "Missing Hyperliquid credentials for account "
        f"{account!r}. Searched environment variables: {', '.join(searched)}. "
        f"Credential file checked: {_hermes_env_path()}"
    )


def _credentialed_accounts() -> list[str]:
    available = _combined_casefold_env()
    pattern = re.compile(r"^HYPERLIQUID_(.+)_(SECRET|WALLET)$", re.IGNORECASE)
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
    return [display[name] for name in sorted(accounts) if {"SECRET", "WALLET"}.issubset(accounts[name])]


def _account_env(account: Optional[str], suffix: str) -> Optional[str]:
    """Compatibility wrapper for wallet/secret credential lookup.

    PRIVATE_KEY resolves from *_SECRET; ACCOUNT_ADDRESS resolves from *_WALLET.
    Variable and account matching are case-insensitive.
    """
    if suffix == "PRIVATE_KEY":
        return _resolve_secret(account)[0]
    if suffix == "ACCOUNT_ADDRESS":
        return _resolve_wallet(account)[0]
    if suffix == "VAULT_ADDRESS":
        value, _selected_key, _source, _searched = _lookup_case_insensitive(_candidate_names(account, "VAULT"))
        return value
    return None


def _safe_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _log_submission(kind: str, request: Mapping[str, Any], sdk_payload: Any) -> None:
    """Log full normalized request and SDK payload before live submission."""
    logger.info(
        "Hyperliquid live submit %s normalized_request=%s structured_request=%s sdk_payload=%s",
        kind,
        _safe_json(dict(request)),
        _safe_json(request.get("structured_request", {})),
        _safe_json(sdk_payload),
    )


# ---------------------------------------------------------------------------
# Hyperliquid bulk_cancel response parsing
# ---------------------------------------------------------------------------
#
# Hyperliquid's `POST /exchange` for a "cancel" action returns:
#
#   {
#     "status": "ok",
#     "response": {
#       "type": "cancel",
#       "data": {
#         "statuses": [
#           {"status": "success"},            # per child
#           {"status": "success", "resting": {"oid": 12345}},
#           ...
#         ]
#       }
#     }
#   }
#
# Server-side rejections come back at the top level:
#
#   {"status": "err", "response": "Signed action over weight limit of 200."}
#   {"status": "err", "response": {"error": "...", "code": ...}}
#
# Rules:
#   - top-level must be a Mapping with status in {"ok", "err"}
#   - "status": "err"  -> failed
#   - "status": "ok":
#       - response must be a Mapping with "data" Mapping
#       - data["statuses"] must be a list with exactly len(chunk) entries
#       - each entry must include "status" == "success" OR mapped "resting"
#         AND must NOT include "error" / "errorMessage"
#       - any other shape -> ambiguous
# Anything outside these rules is ambiguous; ambiguous chunks are treated as
# failure and stop execution immediately (no retry, no inference).


_REDACTED_KEYS = {"signature", "signedAction", "signed_action"}


def _sanitize_exchange_response(raw: Any) -> Any:
    """Strip secret-bearing fields from a stored response for diagnostics."""
    if isinstance(raw, Mapping):
        sanitized: dict[str, Any] = {}
        for key, value in raw.items():
            if key in _REDACTED_KEYS:
                sanitized[key] = "[REDACTED]"
            else:
                sanitized[key] = _sanitize_exchange_response(value)
        return sanitized
    if isinstance(raw, list):
        return [_sanitize_exchange_response(item) for item in raw]
    return raw


def _evaluate_bulk_cancel_response(
    raw: Any, expected_children: int
) -> tuple[str, int, list[dict[str, Any]], dict[str, Any]]:
    """Classify a `bulk_cancel` SDK return value.

    Returns (decision, accepted_count, child_outcomes, parsed) where:
      decision in {"ok", "failed", "ambiguous"}
      accepted_count is the count of children the response explicitly confirms
      child_outcomes is a list of per-child outcome dicts (only populated
        for "ok" decisions where per-child statuses were recognized)
      parsed is the parsed_response summary used for diagnostics
    """
    parsed: dict[str, Any] = {"shape": "unknown"}
    if not isinstance(raw, Mapping):
        parsed["reason"] = "response is not a mapping"
        return ("ambiguous", 0, [], parsed)

    status = raw.get("status")
    if not isinstance(status, str):
        parsed["reason"] = "missing top-level string status"
        return ("ambiguous", 0, [], parsed)
    parsed["status"] = status

    if status.lower() == "err":
        # explicit server-side failure
        body = raw.get("response")
        if isinstance(body, Mapping):
            msg = body.get("error") or body.get("message") or body.get("msg")
        else:
            msg = body
        parsed["error"] = msg
        return ("failed", 0, [], parsed)

    if status.lower() != "ok":
        parsed["reason"] = f"unrecognized top-level status: {status!r}"
        return ("ambiguous", 0, [], parsed)

    # status == "ok" — verify per-child statuses shape.
    response_obj = raw.get("response")
    if not isinstance(response_obj, Mapping):
        parsed["reason"] = "ok status with non-mapping response payload"
        return ("ambiguous", 0, [], parsed)
    data = response_obj.get("data")
    if not isinstance(data, Mapping):
        parsed["reason"] = "ok status with missing/non-mapping response.data"
        return ("ambiguous", 0, [], parsed)
    statuses = data.get("statuses")
    if not isinstance(statuses, list):
        parsed["reason"] = "ok status with missing/non-list response.data.statuses"
        return ("ambiguous", 0, [], parsed)
    if len(statuses) != expected_children:
        parsed["reason"] = (
            f"statuses length {len(statuses)} != submitted children {expected_children}"
        )
        return ("ambiguous", 0, [], parsed)

    accepted = 0
    child_outcomes: list[dict[str, Any]] = []
    ambiguous_child_reasons: list[str] = []
    for index, entry in enumerate(statuses):
        # The Hyperliquid server may surface per-child statuses as either a
        # mapping ({"status": "success", ...}) OR a bare string ("success").
        # Both shapes are accepted; everything else is ambiguous.
        if isinstance(entry, str):
            entry_str = entry.strip().lower()
            if entry_str == "success":
                accepted += 1
                child_outcomes.append({"index": index, "status": "success", "entry": entry})
                continue
            ambiguous_child_reasons.append(f"#{index}: unrecognized string {entry!r}")
            continue
        if not isinstance(entry, Mapping):
            ambiguous_child_reasons.append(f"#{index}: not a mapping")
            continue
        if "error" in entry or "errorMessage" in entry or "error_message" in entry:
            # explicit per-child failure
            child_outcomes.append(
                {
                    "index": index,
                    "status": "error",
                    "reason": entry.get("error")
                    or entry.get("errorMessage")
                    or entry.get("error_message"),
                }
            )
            continue
        entry_status = entry.get("status")
        if entry_status == "success":
            accepted += 1
            child_outcomes.append({"index": index, "status": "success", "entry": dict(entry)})
            continue
        if isinstance(entry_status, str) and entry_status.lower() == "success":
            accepted += 1
            child_outcomes.append({"index": index, "status": "success", "entry": dict(entry)})
            continue
        # Hyperliquid sometimes surfaces per-child resting info without an
        # explicit "status" field. Treat those as confirmed only if the entry
        # looks like {resting: {...}} or matches known success shape.
        if "resting" in entry or "filled" in entry:
            accepted += 1
            child_outcomes.append({"index": index, "status": "success", "entry": dict(entry)})
            continue
        # Unrecognized per-child shape -> ambiguous
        ambiguous_child_reasons.append(f"#{index}: unrecognized shape {dict(entry)!r}")

    if ambiguous_child_reasons:
        parsed["reason"] = "ambiguous per-child status: " + "; ".join(ambiguous_child_reasons[:3])
        return ("ambiguous", 0, [], parsed)

    parsed["accepted_count"] = accepted
    return ("ok", accepted, child_outcomes, parsed)


def _describe_failed_response(raw: Any, expected_children: int) -> str:
    if isinstance(raw, Mapping):
        body = raw.get("response")
        if isinstance(body, Mapping):
            return str(body.get("error") or body.get("message") or body.get("msg") or raw)
        if body:
            return str(body)
    return f"Hyperliquid bulk_cancel returned failure for {expected_children} children"


def _describe_ambiguous_response(raw: Any, expected_children: int) -> str:
    sanitized = _sanitize_exchange_response(raw)
    return (
        f"Ambiguous Hyperliquid bulk_cancel response for {expected_children} children "
        f"(sanitized raw={sanitized!r}); not treated as success"
    )


def _coerce_decimal(value: Any) -> Optional[float]:
    """Best-effort float coercion for matching normalized numeric fields."""
    if value is None:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    if result != result:  # NaN
        return None
    return result


# ---------------------------------------------------------------------------
# Hyperliquid bulk_orders response parsing (placement path)
# ---------------------------------------------------------------------------
#
# Same wire shape as bulk_cancel but the per-child success shape is different:
#
#   {
#     "status": "ok",
#     "response": {
#       "type": "order",
#       "data": {
#         "statuses": [
#           {"status": "success", "oid": <int>},      # created and resting
#           {"status": "waitingForTrigger", "oid": ...},  # conditional / trigger
#           {"error": "Order would immediately trigger"}    # rejected
#         ]
#       }
#     }
#   }
#
# Server-side rejections still come back at the top level:
#
#   {"status": "err", "response": "Signed action over weight limit of 200."}
#
# Recognized per-child success shapes:
#   {"status": "success"}
#   {"status": "resting"}
#   {"status": "waitingForTrigger"}        # a trigger pending; treated as placed
#   {"status": "opened"}                    # position-opening triggered
#   {"status": "filled"}                    # filled immediately
# Recognized per-child failure shapes:
#   {"error": "..."}
#   {"error": "...", "status": "error"}
# Anything else is ambiguous; ambiguous children fail the whole chunk.


def _evaluate_bulk_orders_response(
    raw: Any, expected_children: int
) -> tuple[str, int, list[dict[str, Any]], dict[str, Any]]:
    """Strict classification of a `bulk_orders` SDK return value.

    Returns (decision, accepted_count, child_records, parsed) where:
      decision in {"ok", "failed", "ambiguous"}
      accepted_count counts children confirmed as placed (success/resting/...)
      child_records is a per-child list of dicts with keys:
          index, success, status, oid (optional), error (optional)
      parsed is the diagnostic summary dict.
    """
    parsed: dict[str, Any] = {"shape": "unknown"}
    if not isinstance(raw, Mapping):
        parsed["reason"] = "response is not a mapping"
        return ("ambiguous", 0, [], parsed)

    status = raw.get("status")
    if not isinstance(status, str):
        parsed["reason"] = "missing top-level string status"
        return ("ambiguous", 0, [], parsed)
    parsed["status"] = status

    if status.lower() == "err":
        body = raw.get("response")
        if isinstance(body, Mapping):
            msg = body.get("error") or body.get("message") or body.get("msg")
        else:
            msg = body
        parsed["error"] = msg
        # On a top-level err, no per-child records are available from
        # the exchange; the caller will use chunk_children to populate
        # one unsubmitted record per child.
        return ("failed", 0, [], parsed)
    if status.lower() != "ok":
        parsed["reason"] = f"unrecognized top-level status: {status!r}"
        return ("ambiguous", 0, [], parsed)

    response_obj = raw.get("response")
    if not isinstance(response_obj, Mapping):
        parsed["reason"] = "ok status with non-mapping response payload"
        return ("ambiguous", 0, [], parsed)
    data = response_obj.get("data")
    if not isinstance(data, Mapping):
        parsed["reason"] = "ok status with missing/non-mapping response.data"
        return ("ambiguous", 0, [], parsed)
    statuses = data.get("statuses")
    if not isinstance(statuses, list):
        parsed["reason"] = "ok status with missing/non-list response.data.statuses"
        return ("ambiguous", 0, [], parsed)
    if len(statuses) != expected_children:
        parsed["reason"] = (
            f"statuses length {len(statuses)} != submitted children {expected_children}"
        )
        return ("ambiguous", 0, [], parsed)

    accepted = 0
    child_records: list[dict[str, Any]] = []
    ambiguous_child_reasons: list[str] = []
    for index, entry in enumerate(statuses):
        if not isinstance(entry, Mapping):
            ambiguous_child_reasons.append(f"#{index}: not a mapping")
            child_records.append(
                {"index": index, "success": False, "status": entry,
                 "error": "per-child entry is not a mapping"}
            )
            continue
        # Explicit per-child failure shape.
        if "error" in entry or "errorMessage" in entry or "error_message" in entry:
            error_msg = (
                entry.get("error")
                or entry.get("errorMessage")
                or entry.get("error_message")
            )
            child_records.append(
                {"index": index, "success": False, "status": "error",
                 "error": str(error_msg), "oid": entry.get("oid")}
            )
            # A per-child failure does not fail the whole chunk (we
            # only stop on top-level err / ambiguous). We still record
            # it as not-accepted.
            continue
        entry_status = entry.get("status")
        if entry_status in {"success", "resting", "waitingForTrigger",
                            "opened", "filled"}:
            accepted += 1
            child_records.append(
                {"index": index, "success": True, "status": entry_status,
                 "oid": entry.get("oid")}
            )
            continue
        if "resting" in entry or "filled" in entry:
            accepted += 1
            child_records.append(
                {"index": index, "success": True, "status": "resting",
                 "oid": entry.get("oid")}
            )
            continue
        ambiguous_child_reasons.append(f"#{index}: unrecognized status {entry_status!r}")
        child_records.append(
            {"index": index, "success": False, "status": entry_status,
             "error": "unrecognized per-child status shape"}
        )

    if ambiguous_child_reasons:
        parsed["reason"] = "ambiguous per-child status: " + "; ".join(
            ambiguous_child_reasons[:3]
        )
        return ("ambiguous", 0, [], parsed)

    parsed["accepted_count"] = accepted
    return ("ok", accepted, child_records, parsed)


def _describe_status_error(raw: Any, statuses: list[Any]) -> Optional[str]:
    """Best-effort one-line error string for chunk failure reporting."""
    if isinstance(raw, Mapping) and str(raw.get("status") or "").lower() == "err":
        body = raw.get("response")
        if isinstance(body, Mapping):
            msg = body.get("error") or body.get("message") or body.get("msg")
            if msg:
                return str(msg)
        if body:
            return str(body)
        return "Hyperliquid chunk returned status=err"
    if statuses:
        first = statuses[0]
        if isinstance(first, Mapping):
            err = first.get("error") or first.get("errorMessage")
            if err:
                return f"per-child error: {err}"
    return None


class HyperliquidAgent:
    """Execute normalized Hyperliquid requests via the official SDK."""

    def __init__(self, *, info: Any = None, exchange: Any = None) -> None:
        # Mainnet-only by design. No testnet/simulation/paper mode.
        self.base_url = MAINNET_API_URL
        self._info_override = info
        self._exchange_override = exchange
        self._meta_cache: Optional[dict[str, Any]] = None

    def list_accounts(self) -> dict:
        accounts = discover_accounts("hyperliquid")
        return {
            "success": True,
            "exchange": "hyperliquid",
            "accounts": accounts,
            "message": f"Found {len(accounts)} Hyperliquid configured account(s).",
        }

    def execute(self, request: Mapping[str, Any]) -> dict:
        operation = str(request.get("operation") or "")
        if operation not in SUPPORTED_OPERATIONS:
            return _execution_result(
                request,
                success=False,
                error=f"Unsupported Hyperliquid operation: {operation}",
                normalized_request=dict(request),
                structured_request=dict(request.get("structured_request") or {}),
            )

        try:
            if operation == "order":
                return self._execute_order(request)
            if operation == "batch_orders":
                return self._execute_batch_orders(request)
            if operation == "cancel_orders":
                return self._cancel_orders(request)
            if operation == "balance":
                return self._balance(request)
            if operation == "open_orders":
                return self._open_orders(request)
            if operation == "positions":
                return self._positions(request)
            if operation == "set_tp":
                return self._set_tp_sl(request, "tp")
            if operation == "set_sl":
                return self._set_tp_sl(request, "sl")
        except Exception as exc:
            return _execution_result(
                request,
                success=False,
                error=str(exc),
                error_type=exc.__class__.__name__,
                normalized_request=dict(request),
                structured_request=dict(request.get("structured_request") or {}),
            )

        return _execution_result(request, success=False, error="Unhandled Hyperliquid operation", normalized_request=dict(request))

    # ------------------------------------------------------------------
    # SDK clients
    # ------------------------------------------------------------------
    def _info(self) -> Any:
        if self._info_override is not None:
            return self._info_override
        if Info is None:
            raise RuntimeError("hyperliquid-python-sdk is not installed")
        return Info(self.base_url, skip_ws=True)

    def _exchange(self, request: Mapping[str, Any]) -> Any:
        if self._exchange_override is not None:
            return self._exchange_override
        if Exchange is None or Account is None:
            raise RuntimeError("hyperliquid-python-sdk and eth-account are required")

        account = str(request.get("account") or "") or None
        private_key, selected_secret, secret_searched = _resolve_secret(account)
        account_address, selected_wallet, wallet_searched = _resolve_wallet(account)
        if not private_key or not account_address:
            searched = []
            if not account_address:
                searched.extend(wallet_searched)
            if not private_key:
                searched.extend(secret_searched)
            raise _missing_credentials_error(account, searched)

        wallet = Account.from_key(private_key)
        vault_address = _account_env(account, "VAULT_ADDRESS")
        logger.info(
            "Hyperliquid credential variables selected wallet=%s secret=%s",
            selected_wallet,
            selected_secret,
        )
        return Exchange(
            wallet,
            base_url=self.base_url,
            account_address=account_address,
            vault_address=vault_address,
        )

    def _address(self, request: Mapping[str, Any]) -> str:
        account = str(request.get("account") or "") or None
        configured, _selected_wallet, wallet_searched = _resolve_wallet(account)
        if configured:
            return configured
        private_key, _selected_secret, secret_searched = _resolve_secret(account)
        if private_key and Account is not None:
            return str(Account.from_key(private_key).address)
        raise _missing_credentials_error(account, wallet_searched + secret_searched)

    # ------------------------------------------------------------------
    # Normalized operations
    # ------------------------------------------------------------------
    def _execute_order(self, request: Mapping[str, Any]) -> dict:
        child = dict(request.get("child_order") or {})
        if not child and request.get("child_orders"):
            child = dict(request.get("child_orders", [])[0])
        exchange = self._exchange(request)
        raw, sdk_payload = self._submit_child_order(exchange, request, child)
        child_result = self._child_result(child, True, sdk_payload=sdk_payload, exchange_response=raw)
        return _execution_result(
            request,
            success=True,
            normalized_request=dict(request),
            structured_request=dict(request.get("structured_request") or {}),
            sdk_payload=sdk_payload,
            exchange_response=raw,
            raw_response=raw,
            child_results=[child_result],
        )

    def _execute_batch_orders(self, request: Mapping[str, Any]) -> dict:
        """Place many orders via bounded bulk_orders chunks.

        Behavior:
        - The full child-order sequence is preserved (no reorder / merge / regen).
        - Child orders are split into chunks of at most
          ``HYPERLIQUID_ORDER_CHUNK_SIZE`` and submitted sequentially. Each
          chunk is a single signed ``order`` action via ``bulk_orders``;
          we never retry and never submit later chunks after a failure.
        - Per-child outcomes come from the exchange response. Children that
          the exchange confirms as ``resting`` (or mapping-equal-success)
          count as ``exchange_accepted``. Anything ambiguous counts as
          failure.
        - For every chunk that exchanges says succeeded, we perform a
          read-only ``info.open_orders(address)`` to independently
          verify how many of those children actually ended up resting.
        - ``verified_success`` is True only when every submitted chunk
          succeeded AND every expected child order is present in the
          post-read. ``partial_success`` is True only when at least one
          child order was verified but not all. ``verification_mismatch``
          is True when the exchange accepted some children but the
          post-read found none (or fewer). Everything else is a full
          failure.
        - Failed chunk indices are 0-based internally; the user-facing
          ``failed_chunk_number`` is 1-based.

        Coin-specific rounding is applied via
        ``_round_child_order_for_hyperliquid`` for every child in every
        chunk (size rounded to ``info.meta().universe[*].szDecimals`` and
        price rounded using the same exchange rule we documented for the
        cancel path).
        """
        exchange = self._exchange(request)
        child_orders = [dict(child) for child in request.get("child_orders", [])]

        # Always refresh per-symbol market metadata so coin-specific
        # szDecimals and price max-decimal rounding use the latest
        # exchange truth. ``_round_child_order_for_hyperliquid`` already
        # invokes ``_sz_decimals`` (which lazily caches ``info.meta()``);
        # we explicitly reset here so successive batches in the same
        # session re-read on first call.
        self._meta_cache = None

        chunk_size = HYPERLIQUID_ORDER_CHUNK_SIZE
        total = len(child_orders)
        chunks_planned = (
            0
            if total == 0
            else (total + chunk_size - 1) // chunk_size
        )

        # Empty request: trivially successful with zero-spend semantics.
        if total == 0:
            return _execution_result(
                request,
                success=True,
                normalized_request=dict(request),
                structured_request=dict(request.get("structured_request") or {}),
                sdk_payload={"method": "bulk_orders", "order_requests": []},
                exchange_response=None,
                raw_response=None,
                child_results=[],
                submission_mode="chunked",
                chunk_size=chunk_size,
                total_child_orders=0,
                chunks_planned=0,
                chunks_submitted=0,
                chunks_succeeded=0,
                chunks_failed=0,
                failed_chunk_index=None,
                failed_chunk_number=None,
                failed_chunk_error=None,
                exchange_accepted_count=0,
                verified_resting_count=0,
                remaining_target_count=0,
                verification_mismatch=False,
                partial_success=False,
                verified_success=True,
                verified_rests_performed=True,
                verification_error=None,
                stop_immediately=False,
                retry_attempted=False,
                chunk_results=[],
                chunk_payloads=[],
            )

        chunks = [
            child_orders[i : i + chunk_size]
            for i in range(0, total, chunk_size)
        ]

        aggregated_child_results: list[dict[str, Any]] = []
        chunk_results: list[dict[str, Any]] = []
        chunk_payloads: list[dict[str, Any]] = []

        exchange_accepted_count = 0
        verified_resting_count = 0
        chunks_submitted = 0
        chunks_succeeded = 0
        chunks_failed = 0
        failed_chunk_index: Optional[int] = None
        failed_chunk_number: Optional[int] = None
        failed_chunk_error: Optional[str] = None
        stop_immediately = False
        verification_error: Optional[str] = None

        for chunk_index, chunk_children in enumerate(chunks):
            # Per-child SDK rounding (coin-specific size + price decimals).
            rounded_children = [
                self._round_child_order_for_hyperliquid(child)
                for child in chunk_children
            ]
            order_requests = [
                self._child_to_bulk_sdk_order(child) for child in rounded_children
            ]
            sdk_payload = {
                "method": "bulk_orders",
                "order_requests": order_requests,
            }
            chunk_payloads.append(sdk_payload)
            _log_submission("batch_orders_chunk", request, sdk_payload)

            chunks_submitted += 1
            try:
                raw = exchange.bulk_orders(order_requests)
            except Exception as exc:
                # Exchange-level SDK exception. Stop immediately; do not
                # retry. Mark every child in this chunk as failed; mark
                # any remaining children as unsubmitted.
                chunks_failed += 1
                failed_chunk_index = chunk_index
                failed_chunk_number = chunk_index + 1
                failed_chunk_error = f"SDK exception: {exc!s}"
                stop_immediately = True
                for child in chunk_children:
                    aggregated_child_results.append(
                        self._child_result(
                            child,
                            False,
                            sdk_payload=None,
                            exchange_response=None,
                            status=None,
                            error=failed_chunk_error,
                            chunk_index=chunk_index,
                            chunk_number=chunk_index + 1,
                            submitted=False,
                        )
                    )
                # Append remaining unsubmitted children with no sdk_payload
                for later in chunks[chunk_index + 1 :]:
                    for child in later:
                        aggregated_child_results.append(
                            self._child_result(
                                child,
                                False,
                                sdk_payload=None,
                                exchange_response=None,
                                status=None,
                                error="chunk loop stopped before submission",
                                chunk_index=chunk_index,
                                chunk_number=chunk_index + 1,
                                submitted=False,
                            )
                        )
                chunk_results.append(
                    {
                        "chunk_index": chunk_index,
                        "chunk_number": chunk_index + 1,
                        "submitted_count": len(order_requests),
                        "raw": None,
                        "decision": "exception",
                        "error": failed_chunk_error,
                        "accepted_count": 0,
                    }
                )
                break

            # Strict per-child response evaluation.
            statuses = self._extract_order_statuses(raw)
            decision, accepted_in_chunk, child_records, _parsed = (
                _evaluate_bulk_orders_response(raw, len(order_requests))
            )

            # Build per-child records that preserve the original child
            # sequence and decision. On failure/ambiguous, every child in
            # this chunk counts as not-submitted (the exchange rejected the
            # whole batch). On success, every child was submitted.
            chunk_was_submitted = decision == "ok"
            # When the exchange returned a top-level err or ambiguous
            # shape, child_records is empty; we synthesise one record per
            # chunk child using the failure reason so that the
            # aggregated child_results list still preserves the original
            # sequence.
            if not child_records:
                local_fail_error = (
                    _describe_status_error(raw, statuses)
                    or "Hyperliquid chunk rejected"
                )
                synthetic = []
                for child in chunk_children:
                    synthetic.append(
                        {
                            "index": 0,
                            "success": False,
                            "status": None,
                            "oid": None,
                            "error": local_fail_error,
                        }
                    )
                child_records = synthetic
            chunk_child_results: list[dict[str, Any]] = []
            for child, rounded, record in zip(
                chunk_children, rounded_children, child_records
            ):
                child_res = self._child_result(
                    child,
                    bool(record.get("success")),
                    sdk_payload=self._child_to_bulk_sdk_order(rounded),
                    exchange_response=raw,
                    status=record.get("status"),
                    error=record.get("error"),
                    chunk_index=chunk_index,
                    chunk_number=chunk_index + 1,
                    submitted=chunk_was_submitted,
                )
                # Carry the assigned oid through when the response gave one.
                if record.get("oid") is not None:
                    child_res["oid"] = record["oid"]
                chunk_child_results.append(child_res)
            aggregated_child_results.extend(chunk_child_results)

            chunk_results.append(
                {
                    "chunk_index": chunk_index,
                    "chunk_number": chunk_index + 1,
                    "submitted_count": len(order_requests),
                    "raw": raw,
                    "decision": decision,
                    "accepted_count": accepted_in_chunk,
                    "error": _describe_status_error(raw, statuses),
                }
            )

            if decision != "ok":
                # Strict stop-on-first-failure: no more chunks.
                chunks_failed += 1
                failed_chunk_index = chunk_index
                failed_chunk_number = chunk_index + 1
                failed_chunk_error = (
                    _describe_status_error(raw, statuses) or "Hyperliquid chunk rejected"
                )
                stop_immediately = True
                for later in chunks[chunk_index + 1 :]:
                    for child in later:
                        aggregated_child_results.append(
                            self._child_result(
                                child,
                                False,
                                sdk_payload=None,
                                exchange_response=None,
                                status=None,
                                error="chunk loop stopped before submission",
                                chunk_index=chunk_index,
                                chunk_number=chunk_index + 1,
                                submitted=False,
                            )
                        )
                break

            # Chunk-level exchange-accepted tally.
            chunks_succeeded += 1
            exchange_accepted_count += accepted_in_chunk

            # Independent verification: read open orders and match
            # every accepted child against a resting order. This is a
            # read-only ``info.open_orders(address)`` call; it does not
            # mutate state. Match against the ROUNDED children
            # (post size/price decimal rounding) because the exchange
            # stores the rounded values exactly as submitted.
            verified_chunk, rest_error = self._verify_chunk_resting(
                request, rounded_children
            )
            if rest_error is not None and verification_error is None:
                verification_error = rest_error
            verified_resting_count += verified_chunk

        verified_rests_performed = True

        # Compute remaining-target-count = children never submitted +
        # children submitted-but-not-verified-resting.
        if stop_immediately:
            remaining_unsubmitted = total - chunks_submitted * chunk_size
            if remaining_unsubmitted < 0:
                remaining_unsubmitted = 0
            remaining_target_count = (
                (total - exchange_accepted_count)
                + (total - (chunks_submitted * chunk_size))
            )
            # Clamp to a sane lower bound: at minimum the children
            # exchange accepted but we never verified are 'remaining'
            # unless all chunks were successful, in which case remaining
            # is total - verified.
            remaining_target_count = max(
                remaining_unsubmitted,
                total - verified_resting_count,
            )
        else:
            # All chunks succeeded; remaining = total - verified.
            remaining_target_count = max(total - verified_resting_count, 0)

        # verified_success and partial_success — same semantics as the
        # cancel path: each is driven purely by the verified-vs-matched
        # relationship, never by exchange acceptance alone.
        verified_success = (
            not stop_immediately
            and chunks_failed == 0
            and verified_resting_count == total
            and remaining_target_count == 0
            and verification_error is None
        )
        partial_success = (
            not stop_immediately
            and verified_success is False
            and 0 < verified_resting_count < total
        )
        verification_mismatch = (
            exchange_accepted_count > 0
            and verified_resting_count == 0
            and not stop_immediately is False
        )
        # Refine: verification_mismatch also applies when we tried
        # hard but exchange accepted while post-read disagreed.
        if (
            verification_error is None
            and exchange_accepted_count > 0
            and verified_resting_count == 0
        ):
            verification_mismatch = True

        success = verified_success
        if success:
            error = None
        elif stop_immediately:
            error = (
                f"Stopped at chunk {failed_chunk_number}: {failed_chunk_error}. "
                f"Verified resting: {verified_resting_count}/{total}. "
                f"Unsubmitted: {max(total - chunks_submitted * chunk_size, 0)}."
            )
        elif verification_mismatch:
            error = (
                f"Exchange reported {exchange_accepted_count} accepted orders but "
                f"the independent post-read found zero resting. Verified: "
                f"{verified_resting_count}/{total}."
            )
        elif partial_success:
            error = (
                f"Partial success: {chunks_succeeded}/{chunks_planned} chunks succeeded. "
                f"Verified resting: {verified_resting_count}/{total}. "
                f"Unsubmitted: {max(total - chunks_succeeded * chunk_size, 0)}."
            )
        else:
            error = (
                failed_chunk_error
                or "Hyperliquid ladder placement could not be verified"
            )

        return _execution_result(
            request,
            success=bool(success),
            error=error,
            normalized_request=dict(request),
            structured_request=dict(request.get("structured_request") or {}),
            sdk_payload={
                "method": "bulk_orders",
                "order_requests": [
                    self._child_to_bulk_sdk_order(child)
                    for child in child_orders
                ],
            },
            exchange_response=chunk_results[-1]["raw"] if chunk_results else None,
            raw_response=chunk_results[-1]["raw"] if chunk_results else None,
            child_results=aggregated_child_results,
            submission_mode="chunked",
            chunk_size=chunk_size,
            total_child_orders=total,
            chunks_planned=chunks_planned,
            chunks_submitted=chunks_submitted,
            chunks_succeeded=chunks_succeeded,
            chunks_failed=chunks_failed,
            failed_chunk_index=failed_chunk_index,
            failed_chunk_number=failed_chunk_number,
            failed_chunk_error=failed_chunk_error,
            exchange_accepted_count=exchange_accepted_count,
            verified_resting_count=verified_resting_count,
            remaining_target_count=remaining_target_count,
            verification_mismatch=verification_mismatch,
            partial_success=partial_success,
            verified_success=verified_success,
            verified_rests_performed=verified_rests_performed,
            verification_error=verification_error,
            stop_immediately=stop_immediately,
            retry_attempted=False,
            chunk_results=chunk_results,
            chunk_payloads=chunk_payloads,
        )

    def _submit_child_order(self, exchange: Any, request: Mapping[str, Any], child: Mapping[str, Any]) -> tuple[Any, dict]:
        rounded = self._round_child_order_for_hyperliquid(child)
        order_type = str(rounded.get("order_type") or "").lower()
        symbol = str(rounded.get("symbol") or "").upper()
        is_buy = bool(rounded.get("is_buy"))
        size = float(rounded.get("size"))
        if order_type == "market":
            sdk_payload = {"method": "market_open", "name": symbol, "is_buy": is_buy, "sz": size}
            _log_submission("order", request, sdk_payload)
            return exchange.market_open(symbol, is_buy, size), sdk_payload
        if order_type == "limit":
            price = float(rounded.get("price"))
            sdk_payload = {
                "method": "order",
                "name": symbol,
                "is_buy": is_buy,
                "sz": size,
                "limit_px": price,
                "order_type": {"limit": {"tif": "Gtc"}},
                "reduce_only": bool(rounded.get("reduce_only", False)),
            }
            _log_submission("order", request, sdk_payload)
            return exchange.order(symbol, is_buy, size, price, {"limit": {"tif": "Gtc"}}), sdk_payload
        raise ValueError(f"Unsupported order_type: {order_type}")

    def _round_child_order_for_hyperliquid(self, child_order: Mapping[str, Any]) -> dict:
        """Return a new child order rounded for Hyperliquid SDK submission.

        Hyperliquid enforces size decimals per asset and price max 5 significant
        figures plus max decimals `(6 - szDecimals)` for perps. The previous
        fixed one-decimal price policy rejected BTC ladders such as 64515.2 with
        `Price must be divisible by tick size`.
        """
        rounded = dict(child_order)
        symbol = str(rounded.get("symbol") or "").upper()
        sz_decimals = self._sz_decimals(symbol)
        if "size" in rounded and rounded.get("size") is not None:
            decimals = sz_decimals if sz_decimals is not None else 4
            rounded["size"] = round(float(rounded["size"]), decimals)
        if "price" in rounded and rounded.get("price") is not None:
            rounded["price"] = self._round_hyperliquid_price(float(rounded["price"]), sz_decimals)
        return rounded

    def _sz_decimals(self, symbol: str) -> Optional[int]:
        if not symbol:
            return None
        try:
            if self._meta_cache is None:
                self._meta_cache = self._info().meta()
            universe = self._meta_cache.get("universe") if isinstance(self._meta_cache, Mapping) else []
            for asset in universe or []:
                if isinstance(asset, Mapping) and str(asset.get("name") or "").upper() == symbol:
                    raw_decimals = asset.get("szDecimals")
                    if raw_decimals is not None:
                        return int(raw_decimals)
        except Exception:
            return None
        return None

    @staticmethod
    def _round_hyperliquid_price(price: float, sz_decimals: Optional[int]) -> float:
        significant = float(f"{price:.5g}")
        max_decimals = 6 - int(sz_decimals) if sz_decimals is not None else 1
        return round(significant, max(0, max_decimals))

    def _child_to_bulk_sdk_order(self, child: Mapping[str, Any]) -> dict:
        rounded = self._round_child_order_for_hyperliquid(child)
        order_type = str(rounded.get("order_type") or "").lower()
        if order_type != "limit":
            raise ValueError("bulk_orders currently supports normalized limit child orders only")
        return {
            "coin": str(rounded.get("symbol") or "").upper(),
            "is_buy": bool(rounded.get("is_buy")),
            "sz": float(rounded.get("size")),
            "limit_px": float(rounded.get("price")),
            "order_type": {"limit": {"tif": "Gtc"}},
            "reduce_only": bool(rounded.get("reduce_only", False)),
        }

    @staticmethod
    def _child_result(child: Mapping[str, Any], success: bool, **extra: Any) -> dict:
        result = {
            "child_id": child.get("child_id"),
            "symbol": child.get("symbol"),
            "side": child.get("side"),
            "order_type": child.get("order_type"),
            "size": child.get("size"),
            "price": child.get("price"),
            "success": success,
            "child_order": dict(child),
        }
        result.update(extra)
        return result

    @classmethod
    def _child_result_from_status(
        cls,
        child: Mapping[str, Any],
        sdk_payload: Mapping[str, Any],
        raw: Any,
        statuses: list[Any],
        index: int,
    ) -> dict:
        status = statuses[index] if index < len(statuses) else (statuses[0] if statuses else None)
        success = cls._order_status_success(status, raw)
        result = cls._child_result(child, success, sdk_payload=dict(sdk_payload), exchange_response=raw, status=status)
        if not success:
            result["error"] = cls._order_status_error([status] if status is not None else statuses) or "Hyperliquid order rejected"
        return result

    @staticmethod
    def _extract_order_statuses(raw: Any) -> list[Any]:
        if not isinstance(raw, Mapping):
            return []
        response = raw.get("response")
        if isinstance(response, Mapping):
            data = response.get("data")
            if isinstance(data, Mapping) and isinstance(data.get("statuses"), list):
                return data.get("statuses") or []
        data = raw.get("data")
        if isinstance(data, Mapping) and isinstance(data.get("statuses"), list):
            return data.get("statuses") or []
        return []

    @classmethod
    def _order_status_success(cls, status: Any, raw: Any) -> bool:
        if status is None:
            if isinstance(raw, Mapping) and str(raw.get("status") or "").lower() in {"err", "error", "failed"}:
                return False
            return not bool(cls._order_status_error(cls._extract_order_statuses(raw)))
        if isinstance(status, Mapping):
            return "error" not in status
        return str(status).lower() not in {"err", "error", "failed", "rejected"}

    @staticmethod
    def _order_status_error(statuses: list[Any]) -> Optional[str]:
        for status in statuses:
            if isinstance(status, Mapping) and status.get("error"):
                return str(status.get("error"))
            if isinstance(status, str) and status.lower() in {"err", "error", "failed", "rejected"}:
                return status
        return None

    def _open_orders(self, request: Mapping[str, Any]) -> dict:
        address = self._address(request)
        raw = self._info().open_orders(address)
        orders = self._normalize_open_orders(raw)
        order_summary = self._summarize_orders_by_symbol_side(orders)
        return _execution_result(
            request,
            success=True,
            normalized_request=dict(request),
            structured_request=dict(request.get("structured_request") or {}),
            address=address,
            exchange_response=raw,
            raw_response=raw,
            orders=orders,
            order_summary=order_summary,
            open_order_count=len(orders),
        )

    @classmethod
    def _normalize_open_orders(cls, raw: Any) -> list[dict]:
        if not isinstance(raw, list):
            return []
        orders: list[dict] = []
        for order in raw:
            if not isinstance(order, Mapping):
                continue
            symbol = str(order.get("coin") or order.get("symbol") or "").upper()
            if not symbol:
                continue
            side_raw = order.get("side")
            is_buy_raw = order.get("isBuy")
            side = cls._normalize_order_side(side_raw, is_buy_raw)
            oid = order.get("oid") or order.get("order_id") or order.get("id")
            normalized = {
                "order_id": str(oid) if oid not in (None, "") else None,
                "symbol": symbol,
                "side": side,
                "order_type": order.get("orderType") or order.get("order_type") or order.get("type"),
                "price": order.get("limitPx") or order.get("px") or order.get("price"),
                "size": order.get("sz") or order.get("size") or order.get("origSz"),
                "reduce_only": bool(order.get("reduceOnly") or order.get("reduce_only")),
                "raw_order": dict(order),
            }
            orders.append(normalized)
        return orders

    @staticmethod
    def _normalize_order_side(side_raw: Any, is_buy_raw: Any = None) -> str:
        if isinstance(is_buy_raw, bool):
            return "BUY" if is_buy_raw else "SELL"
        side = str(side_raw if side_raw is not None else is_buy_raw if is_buy_raw is not None else "").strip().lower()
        if side in {"b", "buy", "bid", "true", "1"}:
            return "BUY"
        if side in {"a", "ask", "sell", "false", "0"}:
            return "SELL"
        return str(side_raw or "").upper() or "UNKNOWN"

    @staticmethod
    def _summarize_orders_by_symbol_side(orders: list[dict]) -> list[dict]:
        buckets: dict[str, dict] = {}
        for order in orders:
            if not isinstance(order, Mapping):
                continue
            symbol = str(order.get("symbol") or "UNKNOWN").upper()
            bucket = buckets.setdefault(symbol, {"symbol": symbol, "buy": 0, "sell": 0, "total": 0})
            side = str(order.get("side") or "").upper()
            if side == "BUY":
                bucket["buy"] += 1
            elif side == "SELL":
                bucket["sell"] += 1
            bucket["total"] += 1
        return sorted(buckets.values(), key=lambda item: str(item.get("symbol") or ""))

    # ------------------------------------------------------------------
    # Chunked placement verification
    # ------------------------------------------------------------------

    def _verify_chunk_resting(
        self, request: Mapping[str, Any], chunk_children: list[Mapping[str, Any]]
    ) -> tuple[int, Optional[str]]:
        """Independent post-read verification for one placed chunk.

        Reads ``info.open_orders(address)`` (read-only; no side effects)
        and matches each submitted child against the current resting
        book on a per-symbol basis using ``(coin, side, sz, limit_px,
        reduce_only)``. Returns (verified_count, error_message). On any
        read failure returns ``(0, error_message)``.

        Matching is intentionally tolerant of order shape drift: it
        treats any of ``qty``/``size``/``sz`` as the resting size and
        any of ``px``/``limit_px``/``price`` as the resting price so
        we can verify even if the SDK changes its surface again.
        """
        try:
            address = self._address(request)
            raw = self._info().open_orders(address)
        except Exception as exc:
            return (0, f"open-orders verification failed: {exc!s}")

        resting = self._normalize_open_orders(raw)
        if not isinstance(resting, list):
            return (0, None)

        # Build a per-symbol index of resting orders with their
        # identifying fields normalized.
        resting_index: dict[str, list[dict]] = {}
        for ro in resting:
            if not isinstance(ro, Mapping):
                continue
            sym = str(ro.get("symbol") or "").upper()
            resting_index.setdefault(sym, []).append(
                {
                    "side": str(ro.get("side") or "").upper(),
                    "size": _coerce_decimal(ro.get("size")),
                    "price": _coerce_decimal(ro.get("price")),
                    "reduce_only": bool(ro.get("reduce_only")),
                }
            )

        verified = 0
        for child in chunk_children:
            if not isinstance(child, Mapping):
                continue
            sym = str(child.get("symbol") or "").upper()
            is_buy = bool(child.get("is_buy"))
            side = "BUY" if is_buy else "SELL"
            sz = _coerce_decimal(child.get("size"))
            px = _coerce_decimal(child.get("price"))
            ro = bool(child.get("reduce_only", False))
            pool = resting_index.get(sym, [])
            matched = False
            for entry in pool:
                if entry["side"] != side:
                    continue
                if entry["reduce_only"] != ro:
                    continue
                if entry["size"] != sz:
                    continue
                if entry["price"] != px:
                    continue
                matched = True
                break
            if matched:
                verified += 1
        return (verified, None)

    def _balance(self, request: Mapping[str, Any]) -> dict:
        address = self._address(request)
        raw = self._info().user_state(address)
        margin_summary = raw.get("marginSummary") if isinstance(raw, dict) else None
        return _execution_result(
            request,
            success=True,
            normalized_request=dict(request),
            structured_request=dict(request.get("structured_request") or {}),
            address=address,
            margin_summary=margin_summary,
            exchange_response=raw,
            raw_response=raw,
        )

    def _positions(self, request: Mapping[str, Any]) -> dict:
        address = self._address(request)
        info = self._info()
        raw_state = info.user_state(address)
        try:
            open_orders = info.frontend_open_orders(address)
        except Exception:
            open_orders = info.open_orders(address)
        try:
            all_mids = info.all_mids()
        except Exception:
            all_mids = {}
        positions = self._normalize_positions(raw_state, open_orders, all_mids)
        return _execution_result(
            request,
            success=True,
            normalized_request=dict(request),
            structured_request=dict(request.get("structured_request") or {}),
            address=address,
            positions=positions,
            exchange_response=raw_state,
            open_orders=open_orders,
            raw_response={"user_state": raw_state, "open_orders": open_orders, "all_mids": all_mids},
        )

    def _set_tp_sl(self, request: Mapping[str, Any], kind: str) -> dict:
        price = float(_request_field(request, "price", 0) or 0)
        position_raw = _request_field(request, "position")
        position = position_raw if isinstance(position_raw, Mapping) else {}
        symbol = str(_request_field(request, "symbol") or (position.get("symbol") if isinstance(position, Mapping) else None) or "").upper()
        side = str(_request_field(request, "side") or (position.get("side") if isinstance(position, Mapping) else None) or "").lower()
        action = "Take Profit" if kind == "tp" else "Stop Loss"
        if price == 0:
            address = self._address(request)
            try:
                open_orders = self._info().frontend_open_orders(address)
            except Exception:
                open_orders = self._info().open_orders(address)
            target_prefix = "take" if kind == "tp" else "stop"
            matches = [
                order
                for order in (open_orders if isinstance(open_orders, list) else [])
                if str(order.get("coin") or "").upper() == symbol
                and str(order.get("orderType") or order.get("order_type") or "").lower().startswith(target_prefix)
                and order.get("oid") is not None
            ]
            cancel_requests = [{"coin": symbol, "oid": int(order.get("oid"))} for order in matches]
            if cancel_requests:
                sdk_payload = {"method": "bulk_cancel", "cancel_requests": cancel_requests}
                _log_submission(f"remove_{kind}", request, sdk_payload)
                raw = self._exchange(request).bulk_cancel(cancel_requests)
                return _execution_result(
                    request,
                    success=True,
                    normalized_request=dict(request),
                    structured_request=dict(request.get("structured_request") or {}),
                    message=f"✅ {action} removed for {symbol}",
                    action=kind,
                    symbol=symbol,
                    price=price,
                    cancel_requests=cancel_requests,
                    sdk_payload=sdk_payload,
                    exchange_response=raw,
                    raw_response=raw,
                )
            return _execution_result(
                request,
                success=True,
                normalized_request=dict(request),
                structured_request=dict(request.get("structured_request") or {}),
                message=f"✅ No existing {action} found for {symbol}",
                action=kind,
                symbol=symbol,
                price=price,
                cancel_requests=[],
                sdk_payload=None,
            )
        replace_cancel_requests = self._existing_tpsl_cancel_requests(request, symbol, kind)
        if replace_cancel_requests:
            _log_submission(f"replace_{kind}", request, {"method": "bulk_cancel", "cancel_requests": replace_cancel_requests})
            self._exchange(request).bulk_cancel(replace_cancel_requests)

        child = {
            "child_id": 1,
            "symbol": symbol,
            "side": "sell" if side == "long" else "buy",
            "is_buy": side == "short",
            "order_type": "limit",
            "size": abs(float(position.get("size") or position.get("szi") or 0)),
            "reduce_only": True,
            "price": price,
        }
        rounded = self._round_child_order_for_hyperliquid(child)
        sdk_payload = {
            "method": "order",
            "name": symbol,
            "is_buy": bool(rounded.get("is_buy")),
            "sz": float(rounded.get("size")),
            "limit_px": float(rounded.get("price")),
            "order_type": {"trigger": {"triggerPx": float(rounded.get("price")), "isMarket": True, "tpsl": kind}},
            "reduce_only": True,
        }
        exchange = self._exchange(request)
        _log_submission(f"set_{kind}", request, sdk_payload)
        raw = exchange.order(
            symbol,
            bool(rounded.get("is_buy")),
            float(rounded.get("size")),
            float(rounded.get("price")),
            sdk_payload["order_type"],
            reduce_only=True,
        )
        return _execution_result(
            request,
            success=True,
            normalized_request=dict(request),
            structured_request=dict(request.get("structured_request") or {}),
            message=f"✅ {action} set for {symbol} at {price:g}",
            action=kind,
            symbol=symbol,
            price=price,
            child_order=child,
            replace_cancel_requests=replace_cancel_requests,
            sdk_payload=sdk_payload,
            exchange_response=raw,
            raw_response=raw,
        )

    def _existing_tpsl_cancel_requests(self, request: Mapping[str, Any], symbol: str, kind: str) -> list[dict]:
        address = self._address(request)
        try:
            open_orders = self._info().frontend_open_orders(address)
        except Exception:
            open_orders = self._info().open_orders(address)
        target_prefix = "take" if kind == "tp" else "stop"
        return [
            {"coin": symbol, "oid": int(order.get("oid"))}
            for order in (open_orders if isinstance(open_orders, list) else [])
            if str(order.get("coin") or "").upper() == symbol
            and str(order.get("orderType") or order.get("order_type") or "").lower().startswith(target_prefix)
            and order.get("oid") is not None
        ]

    @staticmethod
    def _normalize_positions(raw_state: Any, open_orders: Any, all_mids: Any) -> list[dict]:
        if not isinstance(raw_state, Mapping):
            return []
        orders = open_orders if isinstance(open_orders, list) else []
        mids = all_mids if isinstance(all_mids, Mapping) else {}
        positions: list[dict] = []
        for item in raw_state.get("assetPositions") or []:
            pos = item.get("position", item) if isinstance(item, Mapping) else {}
            if not isinstance(pos, Mapping):
                continue
            try:
                size = float(pos.get("szi") or 0)
            except Exception:
                size = 0.0
            if size == 0:
                continue
            symbol = str(pos.get("coin") or "").upper()
            side = "long" if size > 0 else "short"
            leverage = pos.get("leverage") if isinstance(pos.get("leverage"), Mapping) else {}
            tps = [order for order in orders if str(order.get("coin") or "").upper() == symbol and str(order.get("orderType") or "").lower().startswith("take")]
            sls = [order for order in orders if str(order.get("coin") or "").upper() == symbol and str(order.get("orderType") or "").lower().startswith("stop")]
            positions.append(
                {
                    "id": f"{symbol}:{side}",
                    "symbol": symbol,
                    "side": side,
                    "size": abs(size),
                    "entry_price": HyperliquidAgent._to_float(pos.get("entryPx")),
                    "mark_price": HyperliquidAgent._to_float(mids.get(symbol)),
                    "unrealized_pnl": HyperliquidAgent._to_float(pos.get("unrealizedPnl")),
                    "roe_pct": HyperliquidAgent._to_float(pos.get("returnOnEquity")),
                    "liquidation_price": HyperliquidAgent._to_float(pos.get("liquidationPx")),
                    "margin_mode": leverage.get("type") or "unknown",
                    "leverage": leverage.get("value"),
                    "take_profit": HyperliquidAgent._order_trigger_price(tps[0]) if tps else None,
                    "stop_loss": HyperliquidAgent._order_trigger_price(sls[0]) if sls else None,
                    "raw_position": dict(pos),
                }
            )
        return positions

    @staticmethod
    def _to_float(value: Any) -> Optional[float]:
        if value in (None, ""):
            return None
        try:
            return float(value)
        except Exception:
            return None

    @staticmethod
    def _order_trigger_price(order: Mapping[str, Any]) -> Optional[float]:
        for key in ("triggerPx", "triggerPxStr", "limitPx"):
            if key in order:
                return HyperliquidAgent._to_float(order.get(key))
        return None

    def _cancel_orders(self, request: Mapping[str, Any]) -> dict:
        """Cancel orders on Hyperliquid using bounded, stop-on-failure chunks.

        Strict semantics:
        - Matched cancel requests are split into chunks of at most
          ``HYPERLIQUID_CANCEL_CHUNK_SIZE`` items.
        - Chunks are submitted sequentially and only once.
        - No automatic retry is performed.
        - A chunk is only counted as successful when the exchange response
          explicitly confirms success for every cancellation in that chunk.
        - On the first ambiguous / failed / excepted chunk, execution stops
          immediately, later chunks are NOT submitted, and a partial-failure
          result is returned.
        - When at least one chunk has succeeded, a read-only post-read of open
          orders is performed to independently verify the cancellation count.
        """
        address = self._address(request)
        open_orders = self._info().open_orders(address)
        matches = self._filter_open_orders(open_orders, request)
        cancel_requests: list[dict[str, Any]] = [
            {"coin": str(order.get("coin") or ""), "oid": int(order.get("oid"))}
            for order in matches
        ]
        target_oids: set[int] = {int(req["oid"]) for req in cancel_requests}

        # Empty target set: nothing to do. Return success-shape no-op.
        if not cancel_requests:
            return _execution_result(
                request,
                success=True,
                normalized_request=dict(request),
                structured_request=dict(request.get("structured_request") or {}),
                address=address,
                canceled_count=0,
                cancel_requests=[],
                matched_order_count=0,
                chunk_size=HYPERLIQUID_CANCEL_CHUNK_SIZE,
                chunks_planned=0,
                chunks_submitted=0,
                chunks_succeeded=0,
                chunks_failed=0,
                exchange_accepted_count=0,
                failed_chunk_index=None,
                failed_chunk_error=None,
                submitted_oid_count=0,
                verified_canceled_count=0,
                remaining_target_count=0,
                verification_performed=True,
                partial_success=False,
                verified_success=True,
                exchange_response={"status": "ok", "response": "no_matching_open_orders"},
                raw_response={"status": "ok", "response": "no_matching_open_orders"},
            )

        # For non-empty target sets, the bulk_cancel SDK return on the last
        # chunk (or the only chunk) is preserved as exchange_response /
        # raw_response. This is primarily for diagnostic continuity with the
        # existing test contract; truthful cancellation reporting lives in
        # verified_canceled_count / remaining_target_count / verified_success.
        last_raw_response: Any = None

        chunks: list[list[dict[str, Any]]] = [
            cancel_requests[i : i + HYPERLIQUID_CANCEL_CHUNK_SIZE]
            for i in range(0, len(cancel_requests), HYPERLIQUID_CANCEL_CHUNK_SIZE)
        ]
        chunks_planned = len(chunks)

        chunk_results: list[dict[str, Any]] = []
        chunks_submitted = 0
        chunks_succeeded = 0
        chunks_failed = 0
        exchange_accepted_count = 0
        failed_chunk_index: Optional[int] = None
        failed_chunk_error: Optional[str] = None
        stop_immediately = False

        exchange = self._exchange(request)

        for chunk_index, chunk in enumerate(chunks):
            sdk_payload = {"method": "bulk_cancel", "cancel_requests": chunk}
            _log_submission("cancel_orders", request, sdk_payload)
            chunks_submitted += 1
            try:
                raw = exchange.bulk_cancel(chunk)
            except Exception as exc:  # SDK raised: do not infer anything
                chunks_failed += 1
                failed_chunk_index = chunk_index
                failed_chunk_error = f"SDK exception: {exc.__class__.__name__}: {exc}"
                stop_immediately = True
                last_raw_response = {"status": "exception", "response": str(exc)}
                chunk_results.append(
                    {
                        "chunk_index": chunk_index,
                        "submitted_count": len(chunk),
                        "raw": {"status": "exception", "response": str(exc)},
                        "decision": "ambiguous",
                        "reason": failed_chunk_error,
                    }
                )
                logger.warning(
                    "Hyperliquid cancel chunk %s raised: %s; stopping immediately",
                    chunk_index,
                    failed_chunk_error,
                )
                break

            decision, accepted_in_chunk, child_outcomes, parsed = _evaluate_bulk_cancel_response(
                raw, len(chunk)
            )
            last_raw_response = raw
            sanitized_raw = _sanitize_exchange_response(raw)
            chunk_results.append(
                {
                    "chunk_index": chunk_index,
                    "submitted_count": len(chunk),
                    "raw": sanitized_raw,
                    "decision": decision,
                    "accepted_count": accepted_in_chunk,
                    "child_outcomes": child_outcomes,
                    "parsed": parsed,
                }
            )

            if decision == "ok":
                chunks_succeeded += 1
                exchange_accepted_count += accepted_in_chunk
                continue

            # decision is "failed" or "ambiguous" -> stop.
            chunks_failed += 1
            failed_chunk_index = chunk_index
            if decision == "ambiguous":
                failed_chunk_error = _describe_ambiguous_response(raw, len(chunk))
            else:
                failed_chunk_error = _describe_failed_response(raw, len(chunk))
            stop_immediately = True
            logger.warning(
                "Hyperliquid cancel chunk %s decision=%s error=%s; stopping immediately",
                chunk_index,
                decision,
                failed_chunk_error,
            )
            break

        submitted_oid_count = sum(
            int(item["submitted_count"]) for item in chunk_results if "submitted_count" in item
        )

        # Read-only post-read of open orders for verification.
        # Per spec: perform a read-only post-read whenever at least one chunk
        # has been submitted or attempted, regardless of whether the chunk
        # loop stopped early. The post-read is independent of the chunk
        # outcome and is what makes verified_canceled_count trustworthy.
        verification_performed = False
        verified_canceled_count = 0
        remaining_target_count = len(target_oids)
        verification_error: Optional[str] = None

        if chunks_submitted > 0:
            try:
                post_open_orders = self._info().open_orders(address)
            except Exception as exc:
                verification_error = f"post-read exception: {exc.__class__.__name__}: {exc}"
            else:
                verification_performed = True
                post_oids: set[int] = set()
                for item in post_open_orders or []:
                    if isinstance(item, Mapping):
                        oid = item.get("oid")
                        if oid is not None:
                            try:
                                post_oids.add(int(oid))
                            except (TypeError, ValueError):
                                continue
                remaining = target_oids & post_oids
                verified_canceled_count = len(target_oids) - len(remaining)
                remaining_target_count = len(remaining)

        partial_success = (
            verification_performed
            and 0 < verified_canceled_count < len(target_oids)
        )
        # verification_mismatch: exchange accepted some cancellations but the
        # independent post-read did not confirm any of them. This is NOT a
        # partial success because we cannot prove any target OID is gone.
        verification_mismatch = (
            verification_performed
            and exchange_accepted_count > 0
            and verified_canceled_count == 0
        )
        verified_success = (
            chunks_succeeded == chunks_planned
            and not stop_immediately
            and verification_performed
            and remaining_target_count == 0
            and verified_canceled_count == len(target_oids)
        )

        # Human-readable 1-based chunk number for Telegram / structured
        # diagnostics. The internal ``failed_chunk_index`` remains 0-based.
        failed_chunk_number: Optional[int] = (
            (failed_chunk_index + 1) if failed_chunk_index is not None else None
        )

        if verification_performed:
            canceled_count_reported = verified_canceled_count
        else:
            canceled_count_reported = 0

        error_out: Optional[str] = None
        if not verified_success:
            if verification_mismatch:
                error_out = (
                    f"Verification mismatch: exchange reported {exchange_accepted_count} "
                    f"accepted cancellations but the independent post-read shows zero "
                    f"target OIDs removed (remaining={remaining_target_count})."
                )
            elif failed_chunk_error and chunks_succeeded == 0:
                error_out = failed_chunk_error
            elif failed_chunk_error and chunks_succeeded > 0:
                error_out = (
                    f"Partial success: {chunks_succeeded}/{chunks_planned} chunks succeeded; "
                    f"failed_chunk_index={failed_chunk_index}; error={failed_chunk_error}"
                )
            elif verification_error:
                error_out = verification_error
            elif verification_performed and remaining_target_count > 0:
                error_out = (
                    f"Verified only {verified_canceled_count}/{len(target_oids)} cancellations; "
                    f"{remaining_target_count} target OIDs remain open."
                )
            else:
                error_out = "Hyperliquid cancellation not verified"

        sdk_payloads = [
            {"method": "bulk_cancel", "cancel_requests": res_item.get("submitted_count")}
            for res_item in chunk_results
        ]

        # Preserve the last exchange response for diagnostics continuity.
        # Truthful cancellation accounting lives in
        # verified_canceled_count / remaining_target_count / verified_success.
        raw_response_diag = _sanitize_exchange_response(last_raw_response) if last_raw_response is not None else None
        exchange_response_diag = _sanitize_exchange_response(last_raw_response) if last_raw_response is not None else None

        return _execution_result(
            request,
            success=bool(verified_success),
            error=error_out,
            normalized_request=dict(request),
            structured_request=dict(request.get("structured_request") or {}),
            address=address,
            canceled_count=canceled_count_reported,
            cancel_requests=cancel_requests,
            target_oids=sorted(target_oids),
            matched_order_count=len(target_oids),
            chunk_size=HYPERLIQUID_CANCEL_CHUNK_SIZE,
            chunks_planned=chunks_planned,
            chunks_submitted=chunks_submitted,
            chunks_succeeded=chunks_succeeded,
            chunks_failed=chunks_failed,
            exchange_accepted_count=exchange_accepted_count,
            failed_chunk_index=failed_chunk_index,
            failed_chunk_number=failed_chunk_number,
            failed_chunk_error=failed_chunk_error,
            submitted_oid_count=submitted_oid_count,
            verified_canceled_count=verified_canceled_count,
            remaining_target_count=remaining_target_count,
            verification_performed=verification_performed,
            verification_error=verification_error,
            partial_success=partial_success,
            verification_mismatch=verification_mismatch,
            verified_success=verified_success,
            chunk_results=chunk_results,
            chunk_payloads=sdk_payloads,
            stop_immediately=stop_immediately,
            retry_attempted=False,
            exchange_response=exchange_response_diag,
            raw_response=raw_response_diag,
        )

    @staticmethod
    def _filter_open_orders(open_orders: Any, request: Mapping[str, Any]) -> list[dict]:
        if not isinstance(open_orders, list):
            return []
        structured = request.get("structured_request") if isinstance(request.get("structured_request"), Mapping) else request
        symbol = str(structured.get("symbol") or "").upper()
        side = str(structured.get("side") or "").lower()
        order_type = str(structured.get("order_type") or "").lower()

        out: list[dict] = []
        for order in open_orders:
            if not isinstance(order, dict):
                continue
            if symbol and symbol != "ALL" and str(order.get("coin") or "").upper() != symbol:
                continue
            if side in {"buy", "sell"}:
                raw_side = str(order.get("side") or order.get("isBuy") or "").lower()
                is_buy = raw_side in {"b", "buy", "true", "1"}
                if side == "buy" and not is_buy:
                    continue
                if side == "sell" and is_buy:
                    continue
            if order_type == "market":
                raw_type = str(order.get("orderType") or order.get("order_type") or "").lower()
                if "market" not in raw_type:
                    continue
            if order.get("oid") is None:
                continue
            out.append(order)
        return out
